"""Alert rules CRUD, idempotent event recording, backfill flag, cooldown, quality pause."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.alerts_engine import Observation, format_message, record_alert_event
from conftest import now_utc


def _rule(client, m, iid, **kw):
    body = {"instrument_id": iid, "metric": "oi_change_pct", "comparator": "gt", "threshold": "2",
            "threshold_unit": "pct", "window_seconds": 300, "cooldown_seconds": 600, **kw}
    r = client.post("/v1/alerts/rules", headers=m.h, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _obs(minutes_ago=0.5, value="2.1", window_min=5, **kw):
    end = now_utc() - timedelta(minutes=minutes_ago)
    return Observation(value=Decimal(value), window_start=end - timedelta(minutes=window_min), window_end=end,
                       observed_at=end, data_quality=kw.pop("quality", "complete"), **kw)


def test_rule_validation_and_crud(client, member, instrument):
    m = member()
    perp, spot = instrument(), instrument("spot")
    bad_unit = client.post("/v1/alerts/rules", headers=m.h, json={
        "instrument_id": perp["instrument_id"], "metric": "oi_change_pct", "threshold": "2", "threshold_unit": "base"})
    assert bad_unit.status_code == 422 and bad_unit.json()["error"]["code"] == "unit_mismatch"
    no_oi = client.post("/v1/alerts/rules", headers=m.h, json={
        "instrument_id": spot["instrument_id"], "metric": "oi_change_pct", "threshold": "2", "threshold_unit": "pct"})
    assert no_oi.status_code == 422 and no_oi.json()["error"]["code"] == "metric_not_available"
    tg = client.post("/v1/alerts/rules", headers=m.h, json={
        "instrument_id": perp["instrument_id"], "metric": "cvd_change", "threshold": "100", "threshold_unit": "base",
        "channel": "telegram"})
    assert tg.status_code == 422 and tg.json()["error"]["code"] == "channel_not_available"
    rule = _rule(client, m, perp["instrument_id"], required_quality="allow_partial")
    assert rule["threshold"] == "2" and rule["channel"] == "in_app" and rule["enabled"] is True
    p = client.patch(f"/v1/alerts/rules/{rule['id']}", headers=m.h, json={"threshold": "2.5", "enabled": False})
    assert p.status_code == 200 and p.json()["threshold"] == "2.5" and p.json()["enabled"] is False
    assert client.patch(f"/v1/alerts/rules/{rule['id']}", headers=m.h, json={"threshold": None}).status_code == 422
    assert [r["id"] for r in client.get("/v1/alerts/rules", headers=m.h).json()] == [rule["id"]]
    assert client.delete(f"/v1/alerts/rules/{rule['id']}", headers=m.h).status_code == 204
    assert client.get(f"/v1/alerts/rules/{rule['id']}", headers=m.h).status_code == 404


def test_event_insertion_is_idempotent_on_dedupe_key(client, app, settings, member, admin, instrument):
    m = member(realtime=True, admin_conn=admin)
    rule = _rule(client, m, instrument()["instrument_id"])
    obs = _obs()
    first = record_alert_event(app.state.db, settings, m.id, rule["id"], obs)
    assert first.outcome == "created" and first.delivery_state == "pending"
    assert first.event["message"] == "OI +2.1%/5m > เกณฑ์ 2%"
    # reconnect / retry / re-evaluation of the same window by a newer engine
    for _ in range(3):
        again = record_alert_event(app.state.db, settings, m.id, rule["id"],
                                   Observation(**{**obs.__dict__, "engine_version": "engine/9.9"}))
        assert again.outcome == "duplicate" and again.event["event_id"] == first.event["event_id"]
    n = admin.execute("SELECT count(*) AS e, (SELECT count(*) FROM alert_deliveries d JOIN alert_events e2"
                      " USING (event_id) WHERE e2.rule_id = %s) AS d FROM alert_events WHERE rule_id = %s",
                      [rule["id"], rule["id"]]).fetchone()
    assert n == {"e": 1, "d": 1}
    ev = client.get(f"/v1/alerts/events/{first.event['event_id']}", headers=m.h).json()
    assert ev["is_backfill"] is False and ev["delivery_policy"] == "live"
    assert ev["deliveries"][0]["state"] == "pending" and ev["deliveries"][0]["channel"] == "in_app"


def test_backfilled_events_are_flagged_and_not_delivered_live(client, app, settings, member, admin, instrument):
    m = member(realtime=True, admin_conn=admin)
    rule = _rule(client, m, instrument()["instrument_id"], cooldown_seconds=0)
    flagged = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=1, is_backfill=True))
    late = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=30))   # old data arriving now
    live = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=0.2, window_min=4))
    assert flagged.event["is_backfill"] and late.event["is_backfill"] and not live.event["is_backfill"]
    assert flagged.delivery_state is None and late.delivery_state is None and live.delivery_state == "pending"
    deliveries = admin.execute("SELECT e.is_backfill, count(d.id) AS n FROM alert_events e LEFT JOIN alert_deliveries d"
                               " USING (event_id) WHERE e.rule_id = %s GROUP BY 1", [rule["id"]]).fetchall()
    assert {r["is_backfill"]: r["n"] for r in deliveries} == {True: 0, False: 1}
    listed = client.get("/v1/alerts/events", headers=m.h, params={"rule_id": rule["id"]}).json()["items"]
    assert [e["is_backfill"] for e in listed] == [False, True, True]          # newest market time first
    live_only = client.get("/v1/alerts/events", headers=m.h,
                           params={"rule_id": rule["id"], "include_backfill": "false"}).json()["items"]
    assert [e["event_id"] for e in live_only] == [str(live.event["event_id"])]


def test_cooldown_quality_pause_disabled_and_threshold(client, app, settings, member, admin, instrument):
    m = member(realtime=True, admin_conn=admin)
    rule = _rule(client, m, instrument()["instrument_id"], cooldown_seconds=600)
    a = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=1, window_min=5))
    b = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=0.5, window_min=4))
    assert a.delivery_state == "pending" and b.delivery_state == "suppressed"
    assert b.event["delivery_policy"] == "suppressed_cooldown"
    gap = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(window_min=3, quality="partial"))
    assert gap.outcome == "paused_data_quality" and gap.event is None
    below = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(value="1.9", window_min=2))
    assert below.outcome == "not_triggered"
    client.patch(f"/v1/alerts/rules/{rule['id']}", headers=m.h, json={"enabled": False})
    off = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(window_min=1))
    assert off.outcome == "rule_disabled"
    other = member()
    with pytest.raises(LookupError):
        record_alert_event(app.state.db, settings, other.id, rule["id"], _obs())


def test_delayed_member_gets_no_live_delivery_and_a_delayed_list(client, app, settings, member, instrument):
    m = member()   # default: delayed
    rule = _rule(client, m, instrument()["instrument_id"])
    recent = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs(minutes_ago=0.5))
    assert recent.event["delivery_policy"] == "suppressed_entitlement" and recent.delivery_state == "suppressed"
    assert client.get("/v1/alerts/events", headers=m.h).json()["items"] == []        # newer than the cut-off
    assert client.get(f"/v1/alerts/events/{recent.event['event_id']}", headers=m.h).status_code == 404


def test_ack_is_idempotent_and_owner_only(client, app, settings, member, admin, instrument):
    m = member(realtime=True, admin_conn=admin)
    rule = _rule(client, m, instrument()["instrument_id"])
    ev = record_alert_event(app.state.db, settings, m.id, rule["id"], _obs()).event
    r1 = client.post(f"/v1/alerts/events/{ev['event_id']}/ack", headers=m.h)
    r2 = client.post(f"/v1/alerts/events/{ev['event_id']}/ack", headers=m.h)
    assert r1.status_code == 200 and r1.json()["acknowledged_at"] == r2.json()["acknowledged_at"] is not None
    assert client.get("/v1/alerts/events", headers=m.h, params={"unacknowledged": "true"}).json()["items"] == []
    other = member(realtime=True, admin_conn=admin)
    assert client.post(f"/v1/alerts/events/{ev['event_id']}/ack", headers=other.h).status_code == 404
    assert client.get("/v1/alerts/events", headers=other.h).json()["items"] == []


def test_message_states_measured_value_and_threshold():
    rule = {"metric": "cvd_change", "threshold_unit": "base", "window_seconds": 300, "comparator": "abs_gt",
            "threshold": Decimal("100")}
    assert format_message(rule, Decimal("-120"), "BTC", "USDT") == "CVD -120 BTC/5m |x| > เกณฑ์ 100 BTC"
