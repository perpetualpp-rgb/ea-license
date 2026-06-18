"""Consensus decision logic.

Mirrors the Doc rule: act only when at least N of the 3 agents agree on a
direction ("อย่างน้อย 2 ใน 3 เงื่อนไข ให้ดำเนินการ"). Ties / no-quorum -> HOLD.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from agents import BUY, HOLD, SELL, Signal


def consensus(signals: List[Signal], required: int = 2) -> Tuple[str, Dict[str, int]]:
    """Return (decision, vote_tally) using a >=`required` majority rule."""
    votes = {BUY: 0, SELL: 0, HOLD: 0}
    for s in signals:
        votes[s.action] += 1
    if votes[BUY] >= required and votes[BUY] >= votes[SELL]:
        return BUY, votes
    if votes[SELL] >= required and votes[SELL] > votes[BUY]:
        return SELL, votes
    return HOLD, votes
