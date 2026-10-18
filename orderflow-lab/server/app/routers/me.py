from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..common import Ctx, get_ctx, iso, resolve_access
from .market import Access

router = APIRouter(prefix="/v1", tags=["account"])

DATA_NOTICE = ("Flow Lab เป็นเครื่องมือวิเคราะห์ ไม่ใช่คำแนะนำการลงทุน ไม่มีจุดเข้า/SL/TP; "
               "ข้อมูลตลาดแสดงตามสิทธิ์ของสมาชิก (สด หรือหน่วง)")


class Me(BaseModel):
    id: uuid.UUID
    issuer: str
    email: Optional[str]
    display_name: Optional[str]
    created_at: str
    market_data: Access
    notice: str


@router.get("/me", response_model=Me, summary="The caller's account and market-data entitlement")
def me(ctx: Ctx = Depends(get_ctx)):
    with ctx.tx() as conn:
        conn.execute("UPDATE users SET last_seen_at = now() WHERE id = %s", [ctx.user_id])
        row = conn.execute("SELECT id, issuer, email, display_name, created_at FROM users WHERE id = %s",
                           [ctx.user_id]).fetchone()
        access = resolve_access(conn, ctx.settings)
    return {**row, "created_at": iso(row["created_at"]), "market_data": access.as_dict(), "notice": DATA_NOTICE}
