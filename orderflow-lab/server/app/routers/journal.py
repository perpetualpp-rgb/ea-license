"""Private journal: observations and trade notes, revisions, immutable market snapshots.

Ownership is enforced twice: every statement filters on owner_id and the transaction
runs with app.user_id set, so RLS hides other members' rows even if a WHERE clause is
missed. Another member's id behaves exactly like an id that does not exist (404).

Retention: DELETE removes the entry and all its revisions permanently and leaves only a
tombstone (entity + id + time, no content) so the member's other devices can drop their
cached copy. Snapshots are immutable and are removed only by the account-deletion purge.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, BaseModel, Field, field_validator

from .. import idempotency
from ..common import Ctx, check_range, canonical_hash, decode_cursor, encode_cursor, get_ctx, iso, \
    require_market_access, resolve_access
from ..errors import ApiError, not_found
from ..snapshots import build_snapshot, snapshot_out

router = APIRouter(prefix="/v1/journal", tags=["journal"])

TEXT_FIELDS = ("title", "facts", "hypothesis", "reason_before_entry", "post_review", "lessons")
EDITABLE = TEXT_FIELDS + ("tags", "market_observed_at", "review_at", "instrument_id", "snapshot_id", "personal_trade_id")
ENTRY_COLS = ("id, owner_id, entry_type, created_at, updated_at, market_observed_at, instrument_id, title, facts,"
              " hypothesis, reason_before_entry, post_review, lessons, tags, snapshot_id, personal_trade_id,"
              " review_at, revision, client_draft_id")
Text = Optional[str]


def _tags(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return v
    out: List[str] = []
    for t in v:
        t = t.strip()
        if not t or len(t) > 40:
            raise ValueError("tags must be 1-40 characters")
        if t not in out:
            out.append(t)
    if len(out) > 20:
        raise ValueError("at most 20 tags")
    return out


class EntryCreate(BaseModel):
    entry_type: Literal["observation", "trade"] = "observation"
    title: str = Field(min_length=1, max_length=200)
    facts: Text = Field(None, max_length=20000, description="what was observed on the chart")
    hypothesis: Text = Field(None, max_length=20000, description="the member's own hypothesis")
    reason_before_entry: Text = Field(None, max_length=20000,
                                      description="reason written before entering; later edits need change_reason")
    post_review: Text = Field(None, max_length=20000, description="review written after the exit")
    lessons: Text = Field(None, max_length=20000)
    tags: List[str] = Field(default_factory=list)
    market_observed_at: Optional[AwareDatetime] = None
    review_at: Optional[AwareDatetime] = None
    instrument_id: Optional[int] = None
    snapshot_id: Optional[uuid.UUID] = None
    personal_trade_id: Optional[uuid.UUID] = None
    client_draft_id: Optional[str] = Field(None, max_length=100)

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v):
        return _tags(v)


class EntryPatch(BaseModel):
    expected_revision: int = Field(ge=1, description="revision the client edited; a mismatch returns 409")
    change_reason: Optional[str] = Field(None, max_length=2000)
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    facts: Text = Field(None, max_length=20000)
    hypothesis: Text = Field(None, max_length=20000)
    reason_before_entry: Text = Field(None, max_length=20000)
    post_review: Text = Field(None, max_length=20000)
    lessons: Text = Field(None, max_length=20000)
    tags: Optional[List[str]] = None
    market_observed_at: Optional[AwareDatetime] = None
    review_at: Optional[AwareDatetime] = None
    instrument_id: Optional[int] = None
    snapshot_id: Optional[uuid.UUID] = None
    personal_trade_id: Optional[uuid.UUID] = None

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v):
        return _tags(v)


class Entry(BaseModel):
    id: uuid.UUID
    entry_type: str
    created_at: str
    updated_at: str
    market_observed_at: Optional[str]
    instrument_id: Optional[int]
    title: str
    facts: Text
    hypothesis: Text
    reason_before_entry: Text
    post_review: Text
    lessons: Text
    tags: List[str]
    snapshot_id: Optional[uuid.UUID]
    personal_trade_id: Optional[uuid.UUID]
    review_at: Optional[str]
    revision: int
    client_draft_id: Optional[str]
    sync_state: str = Field("server_confirmed", description="always server_confirmed: the row is committed")


class EntryPage(BaseModel):
    items: List[Entry]
    next_cursor: Optional[str]


class Revision(BaseModel):
    version: int
    kind: str
    author_id: uuid.UUID
    changed_fields: List[str]
    previous: Dict[str, Any]
    current: Dict[str, Any]
    change_reason: Optional[str]
    created_at: str


class SnapshotCreate(BaseModel):
    instrument_id: int
    tf: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "1m"
    viewport_from: AwareDatetime
    viewport_to: AwareDatetime
    layers: List[str] = Field(default_factory=lambda: ["price", "volume"], max_length=20)
    parameters: Dict[str, Any] = Field(default_factory=dict)
    image_sha256: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$",
                                        description="SHA-256 of the chart image the client will upload (upload not "
                                                    "implemented yet; only the key and hash are stored)")


class Snapshot(BaseModel):
    id: uuid.UUID
    instrument_id: int
    captured_at: str
    source_event_time: Optional[str]
    source_watermark: Optional[str]
    data_access: str
    metric_values: Dict[str, Any]
    parameters: Dict[str, Any]
    engine_version: str
    schema_version: int
    quality_flags: Dict[str, Any]
    chart_viewport: Dict[str, Any]
    chart_layers: List[Any]
    image_object_key: Optional[str]
    image_sha256: Optional[str]
    image_status: str


# -- helpers --------------------------------------------------------------------------
def entry_out(row: dict) -> dict:
    d = dict(row)
    for k in ("created_at", "updated_at", "market_observed_at", "review_at"):
        d[k] = iso(d[k])
    d["tags"] = list(d["tags"] or [])
    d["sync_state"] = "server_confirmed"
    return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return iso(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (list, tuple)):
        return list(v)
    return v


def _check_refs(conn: psycopg.Connection, owner: uuid.UUID, values: Dict[str, Any]) -> None:
    if values.get("snapshot_id") is not None:
        if conn.execute("SELECT 1 FROM market_snapshots WHERE id = %s AND owner_id = %s",
                        [values["snapshot_id"], owner]).fetchone() is None:
            raise ApiError(422, "snapshot_not_found", "snapshot_id does not exist")
    if values.get("personal_trade_id") is not None:
        if conn.execute("SELECT 1 FROM personal_trades WHERE id = %s AND owner_id = %s",
                        [values["personal_trade_id"], owner]).fetchone() is None:
            raise ApiError(422, "trade_not_found", "personal_trade_id does not exist")
    if values.get("instrument_id") is not None:
        if conn.execute("SELECT 1 FROM instruments WHERE instrument_id = %s",
                        [values["instrument_id"]]).fetchone() is None:
            raise ApiError(422, "instrument_not_found", "instrument_id does not exist")


def _get_entry(conn: psycopg.Connection, owner: uuid.UUID, entry_id: uuid.UUID, lock: bool = False) -> dict:
    row = conn.execute(f"SELECT {ENTRY_COLS} FROM journal_entries WHERE id = %s AND owner_id = %s"
                       + (" FOR UPDATE" if lock else ""), [entry_id, owner]).fetchone()
    if row is None:
        raise not_found("journal entry")
    return row


# -- entries --------------------------------------------------------------------------
@router.post("/entries", status_code=201, response_model=Entry,
             responses={200: {"model": Entry, "description": "replay of an earlier request with the same key"},
                        409: {"description": "Idempotency-Key reused with a different body"}},
             summary="Create a journal entry (idempotent with Idempotency-Key)")
def create_entry(body: EntryCreate, ctx: Ctx = Depends(get_ctx),
                 idempotency_key: Optional[str] = Header(None, description=idempotency.HEADER_DESCRIPTION)):
    key = idempotency.check_key(idempotency_key, required=True)
    values = body.model_dump()
    with ctx.tx() as conn:
        existing = idempotency.claim(conn, ctx.user_id, "journal.entry.create", key, canonical_hash(values))
        if existing is not None:
            row = conn.execute(f"SELECT {ENTRY_COLS} FROM journal_entries WHERE id = %s AND owner_id = %s",
                               [existing, ctx.user_id]).fetchone()
            if row is None:
                raise ApiError(410, "resource_deleted", "the entry created with this Idempotency-Key was deleted")
            return JSONResponse(Entry(**entry_out(row)).model_dump(mode="json"), status_code=200,
                                headers={"Idempotent-Replayed": "true"})
        _check_refs(conn, ctx.user_id, values)
        eid = uuid.uuid4()
        cols = ["id", "owner_id", "entry_type", "client_draft_id"] + list(EDITABLE)
        vals = [eid, ctx.user_id, values["entry_type"], values["client_draft_id"]] + [values[c] for c in EDITABLE]
        row = conn.execute(
            f"INSERT INTO journal_entries ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
            f" RETURNING {ENTRY_COLS}", vals).fetchone()
        current = {c: _jsonable(row[c]) for c in EDITABLE}
        conn.execute(
            "INSERT INTO journal_revisions (entry_id, version, owner_id, author_id, kind, changed_fields, current)"
            " VALUES (%s, 1, %s, %s, 'create', %s, %s)",
            [eid, ctx.user_id, ctx.user_id, [c for c in EDITABLE if row[c] not in (None, [])], Jsonb(current)])
        idempotency.bind(conn, ctx.user_id, "journal.entry.create", key, eid)
    return entry_out(row)


@router.get("/entries", response_model=EntryPage, summary="List own entries, newest first (cursor pagination)")
def list_entries(from_: Optional[AwareDatetime] = Query(None, alias="from", description="created_at >= from"),
                 to: Optional[AwareDatetime] = Query(None, description="created_at < to"),
                 entry_type: Optional[Literal["observation", "trade"]] = None,
                 tag: Optional[str] = None,
                 cursor: Optional[str] = None,
                 limit: int = Query(50, ge=1, le=100),
                 ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    c_ts, c_id = decode_cursor(cursor) if cursor else (None, None)
    with ctx.tx() as conn:
        rows = conn.execute(
            f"""
            SELECT {ENTRY_COLS} FROM journal_entries
            WHERE owner_id = %(o)s
              AND (%(f)s::timestamptz IS NULL OR created_at >= %(f)s)
              AND (%(t)s::timestamptz IS NULL OR created_at < %(t)s)
              AND (%(et)s::text IS NULL OR entry_type = %(et)s)
              AND (%(tag)s::text IS NULL OR %(tag)s = ANY(tags))
              AND (%(cts)s::timestamptz IS NULL OR (created_at, id) < (%(cts)s, %(cid)s::uuid))
            ORDER BY created_at DESC, id DESC
            LIMIT %(lim)s
            """, {"o": ctx.user_id, "f": from_, "t": to, "et": entry_type, "tag": tag, "cts": c_ts,
                  "cid": c_id, "lim": limit + 1}).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = encode_cursor(rows[-1]["created_at"], rows[-1]["id"]) if more and rows else None
    return {"items": [entry_out(r) for r in rows], "next_cursor": nxt}


@router.get("/entries/{entry_id}", response_model=Entry, summary="One own entry")
def get_entry(entry_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        return entry_out(_get_entry(conn, ctx.user_id, entry_id))


@router.get("/entries/{entry_id}/revisions", response_model=List[Revision], summary="Full edit history of an entry")
def get_revisions(entry_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        _get_entry(conn, ctx.user_id, entry_id)
        rows = conn.execute(
            "SELECT version, kind, author_id, changed_fields, previous, current, change_reason, created_at"
            " FROM journal_revisions WHERE entry_id = %s AND owner_id = %s ORDER BY version",
            [entry_id, ctx.user_id]).fetchall()
    return [{**r, "created_at": iso(r["created_at"]), "changed_fields": list(r["changed_fields"])} for r in rows]


@router.patch("/entries/{entry_id}", response_model=Entry,
              responses={409: {"description": "expected_revision is not the current revision"}},
              summary="Edit an entry; every change is stored as a new revision")
def patch_entry(entry_id: uuid.UUID, body: EntryPatch, ctx: Ctx = Depends(get_ctx)):
    provided = body.model_fields_set - {"expected_revision", "change_reason"}
    if "title" in provided and body.title is None:
        raise ApiError(422, "validation_error", "title cannot be removed")
    with ctx.tx() as conn:
        row = _get_entry(conn, ctx.user_id, entry_id, lock=True)
        if row["revision"] != body.expected_revision:
            raise ApiError(409, "revision_conflict", "the entry changed since it was loaded",
                           {"current_revision": row["revision"]})
        new = {f: getattr(body, f) for f in provided}
        if "tags" in new and new["tags"] is None:
            new["tags"] = []
        changed = [f for f in EDITABLE if f in new and _jsonable(new[f]) != _jsonable(row[f])]
        if not changed:
            return entry_out(row)
        kind = "edit"
        if "reason_before_entry" in changed:
            if not (body.change_reason and body.change_reason.strip()):
                raise ApiError(422, "change_reason_required",
                               "changing the reason written before entry needs change_reason; "
                               "write the after-exit review in post_review instead")
            kind = "pre_entry_amendment"
        _check_refs(conn, ctx.user_id, {f: new[f] for f in changed})
        sets = ", ".join(f"{f} = %s" for f in changed)
        updated = conn.execute(
            f"UPDATE journal_entries SET {sets}, revision = revision + 1, updated_at = now()"
            f" WHERE id = %s AND owner_id = %s RETURNING {ENTRY_COLS}",
            [new[f] for f in changed] + [entry_id, ctx.user_id]).fetchone()
        conn.execute(
            "INSERT INTO journal_revisions (entry_id, version, owner_id, author_id, kind, changed_fields, previous,"
            " current, change_reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [entry_id, updated["revision"], ctx.user_id, ctx.user_id, kind, changed,
             Jsonb({f: _jsonable(row[f]) for f in changed}), Jsonb({f: _jsonable(updated[f]) for f in changed}),
             body.change_reason])
    return entry_out(updated)


@router.delete("/entries/{entry_id}", status_code=204,
               summary="Delete an entry and its revisions permanently (a content-free tombstone remains)")
def delete_entry(entry_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = conn.execute("DELETE FROM journal_entries WHERE id = %s AND owner_id = %s RETURNING id",
                           [entry_id, ctx.user_id]).fetchone()
        if row is None:
            raise not_found("journal entry")
        conn.execute("INSERT INTO deletion_tombstones (owner_id, entity, entity_id) VALUES (%s, 'journal_entry', %s)"
                     " ON CONFLICT DO NOTHING", [ctx.user_id, entry_id])
    return Response(status_code=204)


# -- snapshots ------------------------------------------------------------------------
@router.post("/snapshots", status_code=201, response_model=Snapshot,
             summary="Freeze what the chart shows now (computed server-side, immutable)")
def create_snapshot(body: SnapshotCreate, ctx: Ctx = Depends(get_ctx),
                    idempotency_key: Optional[str] = Header(None, description=idempotency.HEADER_DESCRIPTION)):
    check_range(body.viewport_from, body.viewport_to)
    key = idempotency.check_key(idempotency_key, required=False)
    with ctx.tx() as conn:
        if key:
            existing = idempotency.claim(conn, ctx.user_id, "journal.snapshot.create", key,
                                         canonical_hash(body.model_dump(mode="json")))
            if existing is not None:
                row = conn.execute("SELECT * FROM market_snapshots WHERE id = %s AND owner_id = %s",
                                   [existing, ctx.user_id]).fetchone()
                return JSONResponse(Snapshot(**snapshot_out(row)).model_dump(mode="json"), status_code=200,
                                    headers={"Idempotent-Replayed": "true"})
        access = require_market_access(resolve_access(conn, ctx.settings))
        row = build_snapshot(conn, ctx.user_id, access, instrument_id=body.instrument_id, tf=body.tf,
                             viewport_from=body.viewport_from, viewport_to=body.viewport_to, layers=body.layers,
                             parameters=body.parameters, image_sha256=body.image_sha256)
        if key:
            idempotency.bind(conn, ctx.user_id, "journal.snapshot.create", key, row["id"])
    return snapshot_out(row)


@router.get("/snapshots/{snapshot_id}", response_model=Snapshot, summary="One own snapshot, exactly as captured")
def get_snapshot(snapshot_id: uuid.UUID, ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        row = conn.execute("SELECT * FROM market_snapshots WHERE id = %s AND owner_id = %s",
                           [snapshot_id, ctx.user_id]).fetchone()
    if row is None:
        raise not_found("snapshot")
    return snapshot_out(row)
