"""Accounting rules (pure Decimal code, no database)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal as D
from fractions import Fraction

import pytest

from app import accounting as acc

T0 = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)


def fill(i, action, qty, price, fee="0", cur="USDT", fx=None, minutes=None, corrects=None):
    return acc.Fill(id=f"f{i}", ts=T0 + timedelta(minutes=i if minutes is None else minutes), action=action,
                    quantity=D(qty), price=D(price), fee=None if fee is None else D(fee), fee_currency=cur,
                    fee_fx_rate=None if fx is None else D(fx), seq=i, corrects_fill_id=corrects)


LONG_USDT = acc.Contract("long", "linear", D(1), "USDT")
SHORT_USDT = acc.Contract("short", "linear", D(1), "USDT")


def test_partial_fills_use_average_entry():
    r = acc.compute_trade(LONG_USDT, [fill(1, "entry", "0.4", "100"), fill(2, "entry", "0.6", "110"),
                                      fill(3, "exit", "1", "120")])
    assert r.avg_entry_price == D("106")
    assert r.gross_pnl == D("14")
    assert r.status == "closed" and r.closed_at == T0 + timedelta(minutes=3)


def test_partial_exits_and_status_progression():
    fills = [fill(1, "entry", "1", "100")]
    assert acc.compute_trade(LONG_USDT, fills).status == "open"
    fills.append(fill(2, "exit", "0.3", "110"))
    r = acc.compute_trade(LONG_USDT, fills)
    assert (r.status, r.gross_pnl, r.open_quantity, r.closed_at) == ("partially_closed", D("3"), D("0.7"), None)
    fills.append(fill(3, "exit", "0.7", "90"))
    r = acc.compute_trade(LONG_USDT, fills)
    assert (r.status, r.gross_pnl, r.open_quantity) == ("closed", D("-4"), D("0"))
    assert acc.compute_trade(LONG_USDT, []).status == "draft"


def test_breakeven_after_tp1_is_net_of_fees():
    risk = acc.InitialRisk(D("20"), "USDT")
    fills = [fill(1, "entry", "2", "100", fee="0.1"), fill(2, "exit", "1", "110", fee="0.05"),
             fill(3, "exit", "1", "100", fee="0.05")]   # stop moved to entry after TP1
    r = acc.compute_trade(LONG_USDT, fills, initial_risk=risk)
    assert r.gross_pnl == D("10") and r.fees == D("0.2") and r.net_pnl == D("9.8")
    assert r.net_r == D("0.49")


def test_exit_at_entry_with_fees_is_a_loss_not_breakeven():
    paid = acc.compute_trade(LONG_USDT, [fill(1, "entry", "1", "100", fee="0.1"), fill(2, "exit", "1", "100", fee="0.1")])
    free = acc.compute_trade(LONG_USDT, [fill(1, "entry", "1", "100"), fill(2, "exit", "1", "100")])
    assert paid.net_pnl == D("-0.2")
    assert free.net_pnl == 0
    stats = acc.compute_stats([acc.ClosedTrade("a", T0, paid), acc.ClosedTrade("b", T0, free)])
    assert stats["counts"]["losses"] == 1 and stats["counts"]["breakeven"] == 1 and stats["counts"]["wins"] == 0
    assert stats["win_rate"] == "0"


def test_fees_in_other_currency_need_a_rate():
    fills = [fill(1, "entry", "1", "100", fee="0.001", cur="BNB", fx="600"), fill(2, "exit", "1", "101", fee="0.1")]
    r = acc.compute_trade(LONG_USDT, fills)
    assert r.fees == D("0.7") and r.net_pnl == D("0.3") and r.complete
    missing = acc.compute_trade(LONG_USDT, [fill(1, "entry", "1", "100", fee="0.001", cur="BNB"),
                                            fill(2, "exit", "1", "101", fee="0.1")])
    assert not missing.complete and missing.incomplete_reasons == ["fee_fx_missing:f1"]
    unknown = acc.compute_trade(LONG_USDT, [fill(1, "entry", "1", "100", fee=None), fill(2, "exit", "1", "101")])
    assert not unknown.complete and unknown.incomplete_reasons == ["fee_unknown:f1"]


def test_funding_and_rebates_enter_net_pnl():
    cash = [acc.Cashflow("c1", T0, "funding", D("0.5"), "USDT"), acc.Cashflow("c2", T0, "funding", D("-0.2"), "USDT"),
            acc.Cashflow("c3", T0, "rebate", D("0.05"), "USDT")]
    r = acc.compute_trade(SHORT_USDT, [fill(1, "entry", "1", "100"), fill(2, "exit", "1", "95")], cash,
                          funding_status="recorded")
    assert (r.gross_pnl, r.funding, r.other_cashflows, r.net_pnl) == (D("5"), D("0.3"), D("0.05"), D("5.35"))
    assert r.complete
    unknown = acc.compute_trade(SHORT_USDT, [fill(1, "entry", "1", "100"), fill(2, "exit", "1", "95")],
                                funding_status="unknown")
    assert unknown.incomplete_reasons == ["funding_unknown"]


def test_inverse_differs_from_linear():
    inverse_long = acc.Contract("long", "inverse", D("100"), "BTC")   # 1 contract = 100 USD
    r = acc.compute_trade(inverse_long, [fill(1, "entry", "10", "50000", cur="BTC"), fill(2, "exit", "10", "55000", cur="BTC")])
    exact = Fraction(1000) * (Fraction(1, 50000) - Fraction(1, 55000))
    assert r.as_dict()["gross_pnl"] == "0.00181818"
    assert acc.q_amount(r.gross_pnl) == (D(exact.numerator) / D(exact.denominator)).quantize(D("1e-8"))
    inverse_short = acc.Contract("short", "inverse", D("100"), "BTC")
    s = acc.compute_trade(inverse_short, [fill(1, "entry", "10", "50000", cur="BTC"), fill(2, "exit", "10", "55000", cur="BTC")])
    assert s.as_dict()["gross_pnl"] == "-0.00181818"
    linear = acc.compute_trade(LONG_USDT, [fill(1, "entry", "0.02", "50000"), fill(2, "exit", "0.02", "55000")])
    assert linear.gross_pnl == D("100")


def test_inverse_average_entry_is_harmonic():
    c = acc.Contract("long", "inverse", D("1"), "BTC")
    r = acc.compute_trade(c, [fill(1, "entry", "100", "40000", cur="BTC"), fill(2, "entry", "100", "60000", cur="BTC"),
                              fill(3, "exit", "200", "48000", cur="BTC")])
    assert acc.q_price(r.avg_entry_price) == D("48000")
    assert r.as_dict()["gross_pnl"] == "0"


def test_contract_multiplier_from_the_broker_spec():
    fills = [fill(1, "entry", "0.5", "2000"), fill(2, "exit", "0.5", "2010")]
    per_lot_100 = acc.compute_trade(acc.Contract("long", "linear", D("100"), "USD"), fills)
    per_unit = acc.compute_trade(acc.Contract("long", "linear", D("1"), "USD"), fills)
    assert per_lot_100.gross_pnl == D("500") and per_unit.gross_pnl == D("5")


def test_decimal_rounding_is_exact_and_done_once():
    r = acc.compute_trade(LONG_USDT, [fill(1, "entry", "0.1", "0.1"), fill(2, "entry", "0.2", "0.2"),
                                      fill(3, "exit", "0.3", "0.3")])
    assert r.gross_pnl == D("0.04") and r.as_dict()["gross_pnl"] == "0.04"
    assert (0.3 * 0.3) - (0.1 * 0.1 + 0.2 * 0.2) != 0.04  # what floats would have produced
    c = acc.Contract("long", "inverse", D("1"), "BTC")
    r = acc.compute_trade(c, [fill(1, "entry", "3", "7", cur="BTC"), fill(2, "exit", "1", "3", cur="BTC"),
                              fill(3, "exit", "1", "31", cur="BTC"), fill(4, "exit", "1", "47", cur="BTC")])
    exits = [Fraction(1, 7) - Fraction(1, p) for p in (3, 31, 47)]

    def q(f):
        return (D(f.numerator) / D(f.denominator)).quantize(D("1e-8"), rounding=ROUND_HALF_EVEN)
    assert r.as_dict()["gross_pnl"] == str(q(sum(exits))) == "0.04170343"
    assert sum(q(x) for x in exits) == D("0.04170344")  # rounding each exit first would drift


def test_net_r_is_na_without_initial_risk():
    fills = [fill(1, "entry", "1", "100"), fill(2, "exit", "1", "110")]
    r = acc.compute_trade(LONG_USDT, fills)
    assert r.net_r is None and r.r_unavailable_reason == "no_initial_risk" and r.as_dict()["net_r"] is None
    fx = acc.compute_trade(LONG_USDT, fills, initial_risk=acc.InitialRisk(D("300"), "THB"))
    assert fx.net_r is None and fx.r_unavailable_reason == "initial_risk_fx_missing"
    ok = acc.compute_trade(LONG_USDT, fills, initial_risk=acc.InitialRisk(D("300"), "THB", D("0.03")))
    assert acc.q_ratio(ok.net_r) == D("1.1111")      # 10 / (300 * 0.03)
    assert ok.as_dict()["net_r"] == "1.1111"


def test_invalid_sequences_are_rejected():
    with pytest.raises(acc.FillSequenceError):
        acc.compute_trade(LONG_USDT, [fill(1, "exit", "1", "100")])
    with pytest.raises(acc.FillSequenceError):
        acc.compute_trade(LONG_USDT, [fill(1, "entry", "1", "100"), fill(2, "exit", "1.5", "101")])


def test_corrections_replace_the_original_fill():
    wrong = fill(1, "entry", "1", "100")
    fixed = fill(2, "entry", "1", "101", minutes=1, corrects="f1")
    fixed_again = fill(3, "entry", "1", "102", minutes=1, corrects="f2")
    exit_ = fill(4, "exit", "1", "110")
    assert acc.compute_trade(LONG_USDT, [wrong, fixed, exit_]).gross_pnl == D("9")
    assert acc.compute_trade(LONG_USDT, [wrong, fixed, fixed_again, exit_]).gross_pnl == D("8")


def _closed(i, net, risk=None, cur="USDT", complete=True):
    res = acc.TradeResult(status="closed", settlement_currency=cur, net_pnl=D(net), gross_pnl=D(net),
                          complete=complete, incomplete_reasons=[] if complete else ["fee_unknown:x"],
                          net_r=None if risk is None else D(net) / D(risk))
    return acc.ClosedTrade(f"t{i}", T0 + timedelta(hours=i), res)


def test_stats_definitions():
    trades = [_closed(1, "10", "5"), _closed(2, "-5", "5"), _closed(3, "3"), _closed(4, "-8", "8"), _closed(5, "0", "4"),
              _closed(6, "100", complete=False), _closed(7, "0.5", "1", cur="BTC")]
    s = acc.compute_stats(trades)
    assert s["counts"] == {"closed": 7, "complete": 6, "incomplete": 1, "wins": 3, "losses": 2, "breakeven": 1,
                           "without_initial_risk": 1}
    assert s["win_rate"] == "0.5"                                # 3 wins / 6 complete (BE in the denominator)
    usdt = s["by_currency"]["USDT"]
    assert usdt["sample"] == 5 and usdt["net_pnl"] == "0" and usdt["expectancy"] == "0"
    assert usdt["profit_factor"] == "1"                          # 13 / 13
    dd = usdt["realized_equity_max_drawdown"]
    assert dd["amount"] == "10" and dd["peak_at"] == "2026-10-01T04:00:00Z" and dd["trough_at"] == "2026-10-01T07:00:00Z"
    assert s["by_currency"]["BTC"]["profit_factor"] is None      # no losses: undefined, not infinite
    # R over trades that have an initial risk: 2, -1, -1, 0, 0.5
    assert s["r"]["sample"] == 5 and s["r"]["mean"] == "0.1" and s["r"]["median"] == "0"
    assert s["incomplete_trades"] == [{"id": "t6", "reasons": ["fee_unknown:x"]}]
    assert acc.compute_stats([])["win_rate"] is None
