"""Journal entries, revisions, deletion and immutable snapshots through the API."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from conftest import now_utc, seed_market, ts


def _entry(client, m, key=None, **body):
    data = {"entry_type": "observation", "title": "CVD แยกทางกับราคา", "facts": "ราคาทำ high ใหม่ แต่ CVD ไม่ทำ",
            "tags": ["cvd", "btc"], **body}
    return client.post("/v1/journal/entries", json=data,
                       headers={**m.h, "Idempotency-Key": key or str(uuid.uuid4())})


def test_retry_with_same_key_creates_one_entry(client, member, admin):
    m = member()
    key = str(uuid.uuid4())
    title = f"retry-{uuid.uuid4().hex[:6]}"
    first = _entry(client, m, key, title=title)
    second = _entry(client, m, key, title=title)
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["sync_state"] == "server_confirmed"
    assert admin.execute("SELECT count(*) AS n FROM journal_entries WHERE owner_id = %s AND title = %s",
                         [m.id, title]).fetchone()["n"] == 1


def test_concurrent_retries_create_one_entry(client, member, admin):
    m = member()
    key = str(uuid.uuid4())
    title = f"race-{uuid.uuid4().hex[:6]}"
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _entry(client, m, key, title=title), range(6)))
    assert sorted(r.status_code for r in results).count(201) == 1
    assert {r.json()["id"] for r in results} == {results[0].json()["id"]}
    assert admin.execute("SELECT count(*) AS n FROM journal_entries WHERE owner_id = %s AND title = %s",
                         [m.id, title]).fetchone()["n"] == 1


def test_same_key_different_body_conflicts(client, member):
    m = member()
    key = str(uuid.uuid4())
    assert _entry(client, m, key, title="a").status_code == 201
    r = _entry(client, m, key, title="b")
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"


def test_key_is_required_and_scoped_per_member(client, member):
    m1, m2 = member(), member()
    r = client.post("/v1/journal/entries", json={"title": "x"}, headers=m1.h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "idempotency_key_required"
    key = str(uuid.uuid4())
    a, b = _entry(client, m1, key), _entry(client, m2, key)
    assert a.status_code == b.status_code == 201 and a.json()["id"] != b.json()["id"]


def test_list_cursor_pagination_and_date_range(client, member, admin):
    m = member()
    ids = [_entry(client, m, title=f"n{i}").json()["id"] for i in range(7)]
    # spread created_at so the date filter has something to cut
    base = now_utc() - timedelta(days=10)
    for i, eid in enumerate(ids):
        admin.execute("UPDATE journal_entries SET created_at = %s WHERE id = %s", [base + timedelta(days=i), eid])
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/journal/entries", params=params, headers=m.h).json()
        seen += [e["id"] for e in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert pages == 3 and seen == list(reversed(ids))
    rng = client.get("/v1/journal/entries", headers=m.h, params={
        "from": (base + timedelta(days=2)).isoformat(), "to": (base + timedelta(days=5)).isoformat()}).json()
    assert [e["id"] for e in rng["items"]] == list(reversed(ids[2:5]))
    assert client.get("/v1/journal/entries", headers=m.h, params={"from": "2026-10-01T00:00:00"}).status_code == 422
    assert client.get("/v1/journal/entries", headers=m.h, params={"cursor": "garbage"}).status_code == 400


def test_patch_creates_revisions_and_protects_pre_entry_reason(client, member, admin):
    m = member()
    e = _entry(client, m, entry_type="trade", reason_before_entry="แนวรับ + CVD ฝั่งซื้อ").json()
    eid = e["id"]
    r = client.patch(f"/v1/journal/entries/{eid}", headers=m.h,
                     json={"expected_revision": 1, "post_review": "ออกตามแผน"})
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert r.json()["reason_before_entry"] == "แนวรับ + CVD ฝั่งซื้อ"
    # changing the pre-entry reason needs a stated reason, and is labelled as such
    bad = client.patch(f"/v1/journal/entries/{eid}", headers=m.h,
                       json={"expected_revision": 2, "reason_before_entry": "เขียนใหม่"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "change_reason_required"
    ok = client.patch(f"/v1/journal/entries/{eid}", headers=m.h,
                      json={"expected_revision": 2, "reason_before_entry": "แนวรับ", "change_reason": "พิมพ์ผิด"})
    assert ok.status_code == 200 and ok.json()["revision"] == 3
    stale = client.patch(f"/v1/journal/entries/{eid}", headers=m.h, json={"expected_revision": 2, "lessons": "x"})
    assert stale.status_code == 409 and stale.json()["error"]["details"]["current_revision"] == 3
    noop = client.patch(f"/v1/journal/entries/{eid}", headers=m.h, json={"expected_revision": 3, "post_review": "ออกตามแผน"})
    assert noop.json()["revision"] == 3
    revs = client.get(f"/v1/journal/entries/{eid}/revisions", headers=m.h).json()
    assert [(v["version"], v["kind"]) for v in revs] == [(1, "create"), (2, "edit"), (3, "pre_entry_amendment")]
    assert revs[2]["previous"] == {"reason_before_entry": "แนวรับ + CVD ฝั่งซื้อ"}
    assert revs[2]["current"] == {"reason_before_entry": "แนวรับ"} and revs[2]["change_reason"] == "พิมพ์ผิด"
    assert revs[1]["changed_fields"] == ["post_review"]
    assert admin.execute("SELECT count(*) AS n FROM journal_revisions WHERE entry_id = %s", [eid]).fetchone()["n"] == 3


def test_delete_removes_entry_and_revisions_and_leaves_tombstone(client, member, admin):
    m = member()
    key = str(uuid.uuid4())
    eid = _entry(client, m, key).json()["id"]
    client.patch(f"/v1/journal/entries/{eid}", headers=m.h, json={"expected_revision": 1, "lessons": "x"})
    assert client.delete(f"/v1/journal/entries/{eid}", headers=m.h).status_code == 204
    assert client.get(f"/v1/journal/entries/{eid}", headers=m.h).status_code == 404
    assert admin.execute("SELECT count(*) AS n FROM journal_revisions WHERE entry_id = %s", [eid]).fetchone()["n"] == 0
    t = admin.execute("SELECT entity FROM deletion_tombstones WHERE entity_id = %s", [eid]).fetchone()
    assert t == {"entity": "journal_entry"}
    assert _entry(client, m, key).status_code == 410   # replaying the create does not resurrect it
    assert client.delete(f"/v1/journal/entries/{eid}", headers=m.h).status_code == 404


def test_snapshot_is_server_computed_and_frozen(client, member, admin, instrument):
    m = member()
    inst = instrument()
    seed = seed_market(admin, inst, minutes=90)
    vp_from = seed["start"]
    vp_to = seed["start"] + timedelta(minutes=30)
    body = {"instrument_id": inst["instrument_id"], "tf": "1m", "viewport_from": vp_from.isoformat(),
            "viewport_to": vp_to.isoformat(), "layers": ["price", "volume", "cvd"], "parameters": {"cvd_anchor": "viewport"},
            "image_sha256": "ab" * 32}
    r = client.post("/v1/journal/snapshots", json=body, headers=m.h)
    assert r.status_code == 201, r.text
    snap = r.json()
    mv = snap["metric_values"]
    assert mv["kline_1m"]["candles"] == 30 and mv["kline_1m"]["volume"] == "315"          # 30 x 10.5
    assert mv["kline_1m"]["taker_sell_volume"] == "127.5"                                  # 30 x (10.5 - 6.25)
    assert mv["trade_tape"]["trades"] == 60 and mv["trade_tape"]["cvd_unit"] == "BTC"
    assert mv["trade_tape"]["unknown_side_volume"] == "2.5"   # every third trade has an unknown side
    assert snap["quality_flags"]["kline_complete"] is True and snap["data_access"] == "delayed"
    assert snap["image_object_key"] == f"snapshots/{m.id}/{snap['id']}.png"
    assert snap["image_status"] == "upload_not_implemented"
    # late trades arrive for the same window: the stored snapshot does not change
    admin.execute("INSERT INTO trades (venue, market_type, symbol, agg_trade_id, instrument_id, event_ts, price, qty,"
                  " taker_side) VALUES (%s, %s, %s, 999999, %s, %s, 100, 50, 'buy')",
                  [inst["venue"], inst["market_type"], inst["symbol"], inst["instrument_id"], vp_from + timedelta(minutes=1)])
    again = client.get(f"/v1/journal/snapshots/{snap['id']}", headers=m.h).json()
    assert again == snap
    fresh = client.post("/v1/journal/snapshots", json=body, headers=m.h).json()
    assert fresh["id"] != snap["id"]
    assert Decimal(fresh["metric_values"]["trade_tape"]["cvd"]) == Decimal(mv["trade_tape"]["cvd"]) + 50
    # no route changes a snapshot
    assert client.patch(f"/v1/journal/snapshots/{snap['id']}", json={}, headers=m.h).status_code == 405
    assert client.delete(f"/v1/journal/snapshots/{snap['id']}", headers=m.h).status_code == 405
    # an entry can point at it; another member cannot read it or point at it
    e = _entry(client, m, snapshot_id=snap["id"], instrument_id=inst["instrument_id"])
    assert e.status_code == 201 and e.json()["snapshot_id"] == snap["id"]
    other = member()
    assert client.get(f"/v1/journal/snapshots/{snap['id']}", headers=other.h).status_code == 404
    stolen = _entry(client, other, snapshot_id=snap["id"])
    assert stolen.status_code == 422 and stolen.json()["error"]["code"] == "snapshot_not_found"


def test_snapshot_respects_delayed_cutoff(client, member, admin, instrument):
    m = member()
    inst = instrument()
    seed_market(admin, inst, minutes=40)
    now = now_utc()
    body = {"instrument_id": inst["instrument_id"], "viewport_from": (now - timedelta(minutes=10)).isoformat(),
            "viewport_to": now.isoformat()}
    r = client.post("/v1/journal/snapshots", json=body, headers=m.h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "viewport_after_data_cutoff"
    body["viewport_from"] = (now - timedelta(minutes=30)).isoformat()
    snap = client.post("/v1/journal/snapshots", json=body, headers=m.h).json()
    cutoff = now - timedelta(minutes=15)
    assert snap["quality_flags"]["clamped_to_cutoff"] is True
    assert ts(snap["source_event_time"]) < cutoff
    assert ts(snap["chart_viewport"]["to"]) <= now_utc() - timedelta(minutes=15)
    assert ts(snap["source_watermark"]) == ts(snap["chart_viewport"]["to"])


def test_patch_with_null_tags_clears_them_instead_of_failing(client, member):
    m = member()
    eid = _entry(client, m).json()["id"]
    r = client.patch(f"/v1/journal/entries/{eid}", headers=m.h, json={"expected_revision": 1, "tags": None})
    assert r.status_code == 200 and r.json()["tags"] == [] and r.json()["revision"] == 2
    assert client.patch(f"/v1/journal/entries/{eid}", headers=m.h,
                        json={"expected_revision": 2, "title": None}).status_code == 422
