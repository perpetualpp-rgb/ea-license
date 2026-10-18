"""Export of the caller's own journal and trades (never another member's, never raw
market data). JSON carries everything including revisions and versions; CSV is one
table per request (dataset=journal_entries | trades | fills)."""
from __future__ import annotations

import csv
import io
import json
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import AwareDatetime

from ..common import Ctx, check_range, dstr, get_ctx, iso, utcnow
from ..config import API_VERSION
from .journal import ENTRY_COLS, entry_out
from .trades import CASH_COLS, FILL_COLS, TRADE_COLS, load_children, trade_out
from ..snapshots import snapshot_out

router = APIRouter(prefix="/v1", tags=["export"])
EXPORT_SCHEMA_VERSION = 1
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(v: Any) -> Any:
    """Neutralise spreadsheet formulas in text cells (CSV injection)."""
    if isinstance(v, str) and v.startswith(FORMULA_START):
        try:
            float(v)
            return v  # a plain number such as -12.5
        except ValueError:
            return "'" + v
    return v


def _csv(header: List[str], rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else _cell(v) for v in r])
    return "﻿" + buf.getvalue()  # BOM so spreadsheet apps read Thai text as UTF-8


@router.get("/export", summary="Download own journal entries and trades as CSV or JSON",
            responses={200: {"content": {"text/csv": {}, "application/json": {}}}})
def export(format: Literal["csv", "json"] = "json",
           from_: Optional[AwareDatetime] = Query(None, alias="from", description="created_at >= from"),
           to: Optional[AwareDatetime] = Query(None, description="created_at < to"),
           dataset: Literal["journal_entries", "trades", "fills"] = Query(
               "journal_entries", description="CSV only: which table to export"),
           ctx: Ctx = Depends(get_ctx)):
    check_range(from_, to)
    rng = {"o": ctx.user_id, "f": from_, "t": to}
    where = ("owner_id = %(o)s AND (%(f)s::timestamptz IS NULL OR created_at >= %(f)s)"
             " AND (%(t)s::timestamptz IS NULL OR created_at < %(t)s)")
    with ctx.tx() as conn:
        entries = conn.execute(f"SELECT {ENTRY_COLS} FROM journal_entries WHERE {where} ORDER BY created_at, id",
                               rng).fetchall()
        trades = conn.execute(f"SELECT {TRADE_COLS} FROM personal_trades WHERE {where} ORDER BY created_at, id",
                              rng).fetchall()
        fills, cash = load_children(conn, ctx.user_id, [t["id"] for t in trades])
        revisions = conn.execute(
            "SELECT entry_id, version, kind, changed_fields, previous, current, change_reason, created_at"
            " FROM journal_revisions WHERE owner_id = %s AND entry_id = ANY(%s) ORDER BY entry_id, version",
            [ctx.user_id, [e["id"] for e in entries]]).fetchall()
        snap_ids = [e["snapshot_id"] for e in entries if e["snapshot_id"]]
        snaps = conn.execute(
            "SELECT * FROM market_snapshots WHERE owner_id = %s AND (id = ANY(%s) OR"
            " ((%s::timestamptz IS NULL OR captured_at >= %s) AND (%s::timestamptz IS NULL OR captured_at < %s)))"
            " ORDER BY captured_at, id", [ctx.user_id, snap_ids, from_, from_, to, to]).fetchall()
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    if format == "json":
        by_entry: dict = {}
        for r in revisions:
            by_entry.setdefault(r["entry_id"], []).append(
                {**{k: r[k] for k in ("version", "kind", "previous", "current", "change_reason")},
                 "changed_fields": list(r["changed_fields"]), "created_at": iso(r["created_at"])})
        doc = {
            "export_schema_version": EXPORT_SCHEMA_VERSION, "api_version": API_VERSION,
            "exported_at": iso(utcnow()), "owner_id": str(ctx.user_id),
            "range": {"from": iso(from_), "to": iso(to), "basis": "created_at"},
            "journal_entries": [{**entry_out(e), "id": str(e["id"]), "revisions": by_entry.get(e["id"], [])}
                                for e in entries],
            "market_snapshots": [snapshot_out(s) for s in snaps],
            "personal_trades": [trade_out(t, fills[t["id"]], cash[t["id"]], detail=True) for t in trades],
            "attachments": "snapshot images are referenced by image_object_key/image_sha256; upload/download is "
                           "not implemented yet",
        }
        body = json.dumps(doc, ensure_ascii=False, default=str, indent=1)
        return Response(body, media_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="flowlab-export-{stamp}.json"'})
    if dataset == "journal_entries":
        header = ["id", "entry_type", "created_at", "updated_at", "market_observed_at", "instrument_id", "title",
                  "facts", "hypothesis", "reason_before_entry", "post_review", "lessons", "tags", "snapshot_id",
                  "personal_trade_id", "review_at", "revision"]
        rows = [[e["id"], e["entry_type"], iso(e["created_at"]), iso(e["updated_at"]), iso(e["market_observed_at"]),
                 e["instrument_id"], e["title"], e["facts"], e["hypothesis"], e["reason_before_entry"],
                 e["post_review"], e["lessons"], "|".join(e["tags"] or []), e["snapshot_id"],
                 e["personal_trade_id"], iso(e["review_at"]), e["revision"]] for e in entries]
    elif dataset == "trades":
        header = ["id", "venue_label", "symbol", "market_type", "contract_type", "contract_multiplier", "side",
                  "status", "opened_at", "closed_at", "settlement_currency", "gross_pnl", "fees", "funding",
                  "other_cashflows", "net_pnl", "initial_risk_amount", "initial_risk_currency", "net_r",
                  "complete", "incomplete_reasons", "source"]
        rows = []
        for t in trades:
            d = trade_out(t, fills[t["id"]], cash[t["id"]])
            s = d["summary"] or {}
            rows.append([t["id"], t["venue_label"], t["symbol"], t["market_type"], t["contract_type"],
                         d["contract_multiplier"], t["side"], t["status"], d["opened_at"], d["closed_at"],
                         t["settlement_currency"], s.get("gross_pnl"), s.get("fees"), s.get("funding"),
                         s.get("other_cashflows"), s.get("net_pnl"), d["initial_risk_amount"],
                         t["initial_risk_currency"], s.get("net_r"), s.get("complete"),
                         "|".join(s.get("incomplete_reasons") or []), t["source"]])
    else:
        header = ["id", "trade_id", "ts", "action", "quantity", "price", "fee", "fee_currency", "fee_fx_rate",
                  "import_key", "corrects_fill_id", "correction_reason"]
        rows = [[f["id"], f["trade_id"], iso(f["ts"]), f["action"], dstr(f["quantity"]), dstr(f["price"]),
                 dstr(f["fee"]), f["fee_currency"], dstr(f["fee_fx_rate"]), f["import_key"], f["corrects_fill_id"],
                 f["correction_reason"]] for t in trades for f in fills[t["id"]]]
    return Response(_csv(header, rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="flowlab-{dataset}-{stamp}.csv"'})
