"""live.check coverage: measured against the scheduled window, never first-to-last."""
import os
import tempfile
import unittest

from live.check import HOLE_S, coverage, day_start, render
from live.sink import HourlySink

DAY = "2026-10-18"
D0 = day_start(DAY)          # 2026-10-18 00:00:00 UTC
S, M, H, DAY_MS = 1000, 60_000, 3_600_000, 86_400_000
MK, SYM = "futures", "XAUUSDT"


def write(root, stream, rows, filed_at=None):
    """Write rows as the collector would; ``filed_at`` overrides the partition time (late backfill)."""
    sink = HourlySink(root)
    for r in rows:
        sink.write(MK, SYM, stream, r[0] if filed_at is None else filed_at, r)
    sink.close()


def run_collector(root, start, end):
    """What a collector running from ``start`` to ``end`` leaves: a book row every second,
    a closed candle every minute and one trade per minute."""
    write(root, "book", [[t, 1, 100.0, [], []] for t in range(start, end, S)])
    write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0] for t in range(start, end - M + 1, M)])
    write(root, "trades", [[t + 5, (t - D0) // M, 100.0, 1.0, 0] for t in range(start, end, M)])


class Coverage(unittest.TestCase):
    def test_one_row_each_is_near_zero_not_100(self):
        with tempfile.TemporaryDirectory() as root:
            t = D0 + 10 * H
            write(root, "book", [[t, 1, 100.0, [], []]])
            write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0]])
            write(root, "trades", [[t, 42, 100.0, 1.0, 0]])
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertAlmostEqual(c["book"]["pct"], 100.0 / 86_400)
            self.assertAlmostEqual(c["kline"]["pct"], 100.0 / 1_440)
            self.assertEqual((c["book"]["expected"], c["kline"]["expected"]), (86_400, 1_440))
            self.assertEqual(c["trades"]["id_continuity_pct"], 100.0)   # one id: continuous, but that is not uptime
            self.assertEqual(c["book"]["holes"], [(D0, t), (t + S, D0 + DAY_MS)])
            self.assertIn("ไม่ใช่ uptime", render(MK, SYM, c))

    def test_empty_day_is_na(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "book", [[D0 - H, 1, 100.0, [], []]])          # data on another day only
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertFalse(c["evidence"])
            self.assertEqual((c["book"]["pct"], c["kline"]["pct"], c["trades"]["id_continuity_pct"]), (None, None, None))
            self.assertEqual(c["book"]["holes"], [])

    def test_stream_without_rows_on_a_day_with_evidence_is_zero(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0] for t in range(D0, D0 + DAY_MS, M)])
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertEqual(c["kline"]["pct"], 100.0)
            self.assertEqual(c["book"]["pct"], 0.0)                     # collector ran, book never synced
            self.assertEqual(c["book"]["holes"], [(D0, D0 + DAY_MS)])

    def test_stopped_at_start_of_day_shows_downtime(self):
        with tempfile.TemporaryDirectory() as root:
            run_collector(root, D0 + 12 * H, D0 + DAY_MS)                # started at noon
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertAlmostEqual(c["book"]["pct"], 50.0)
            self.assertAlmostEqual(c["kline"]["pct"], 50.0)
            self.assertEqual(c["book"]["holes"], [(D0, D0 + 12 * H)])
            self.assertEqual(c["trades"]["id_continuity_pct"], 100.0)   # the observed span is complete: not uptime

    def test_stopped_at_end_of_day_shows_downtime(self):
        with tempfile.TemporaryDirectory() as root:
            run_collector(root, D0, D0 + 6 * H)                          # died at 06:00
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertAlmostEqual(c["book"]["pct"], 25.0)
            self.assertEqual(c["book"]["holes"], [(D0 + 6 * H, D0 + DAY_MS)])
            self.assertEqual(c["book"]["age_s"], (2 * DAY_MS - 6 * H + S) / 1000)   # stale: newest book row is 06:00 minus 1 s
            self.assertAlmostEqual(c["kline"]["pct"], 25.0)

    def test_short_holes_count_against_coverage_but_are_not_listed(self):
        with tempfile.TemporaryDirectory() as root:
            rows = [[t, 1, 100.0, [], []] for t in range(D0, D0 + DAY_MS, S) if not (D0 + H <= t < D0 + H + 30 * S)
                    and not (D0 + 5 * H <= t < D0 + 5 * H + HOLE_S * S)]
            write(root, "book", rows)
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)
            self.assertEqual(c["book"]["seconds"], 86_400 - 30 - HOLE_S)
            self.assertEqual(c["book"]["holes"], [(D0 + 5 * H, D0 + 5 * H + HOLE_S * S)])

    def test_backfilled_rows_in_next_days_files_count_for_their_event_day(self):
        with tempfile.TemporaryDirectory() as root:
            write(root, "trades", [[D0 + 10 * H, 1, 100.0, 1.0, 0], [D0 + 10 * H + 1, 3, 100.0, 1.0, 0]])
            # id 2 was backfilled the next morning, so it is filed under 2026-10-19
            write(root, "trades", [[D0 + 10 * H, 2, 100.0, 1.0, 0]], filed_at=D0 + DAY_MS + 7 * H)
            # the 23:59 candle closes at midnight: filed by its close time on the next day
            write(root, "kline", [[D0 + DAY_MS - M, 1, 1, 1, 1, 0, 0, 0]], filed_at=D0 + DAY_MS + 1)
            self.assertTrue(os.path.isdir(os.path.join(root, MK, SYM, "trades", "2026-10-19")))
            c = coverage(root, MK, SYM, DAY, now_ms=D0 + 3 * DAY_MS)
            self.assertEqual((c["trades"]["rows"], c["trades"]["id_continuity_pct"]), (3, 100.0))
            self.assertEqual(c["kline"]["minutes"], 1)
            nxt = coverage(root, MK, SYM, "2026-10-19", now_ms=D0 + 3 * DAY_MS)
            self.assertEqual((nxt["trades"]["rows"], nxt["kline"]["minutes"]), (0, 0))   # not double counted

    def test_today_is_clipped_to_now_and_future_is_na(self):
        with tempfile.TemporaryDirectory() as root:
            now = D0 + 6 * H + 30 * S
            run_collector(root, D0, now)
            c = coverage(root, MK, SYM, DAY, now_ms=now)
            self.assertEqual((c["window"]["start"], c["window"]["end"]), (D0, now))
            self.assertEqual((c["book"]["expected"], c["kline"]["expected"]), (6 * 3600 + 30, 360))
            self.assertEqual(c["book"]["pct"], 100.0)
            self.assertEqual(c["kline"]["pct"], 100.0)
            future = coverage(root, MK, SYM, "2026-10-19", now_ms=now)
            self.assertIsNone(future["window"])
            self.assertEqual((future["book"]["pct"], future["kline"]["pct"]), (None, None))
            self.assertIn("N/A", render(MK, SYM, future))

    def test_rest_klines_are_not_websocket_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            start = D0 + 12 * H                                          # fresh start at noon: 24 h of history over REST
            lost = range(D0 + 18 * H, D0 + 18 * H + 5 * M, M)            # socket down 18:00-18:05
            write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0] for t in range(start, D0 + DAY_MS, M) if t not in lost])
            write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0] for t in range(start - DAY_MS, start, M)], filed_at=start)
            write(root, "kline", [[t, 1, 1, 1, 1, 0, 0, 0] for t in lost[:3]], filed_at=D0 + 18 * H + 6 * M)
            write(root, "meta", [[start, "connected"], [start + S, "kline_history", start - DAY_MS, start - M, 1440],
                                 [D0 + 18 * H + 6 * M, "kline_backfill", lost[0], lost[2], 3],
                                 [D0 + 18 * H + 6 * M, "kline_gap", lost[3], lost[4], "REST failed 6 times: timed out"]])
            k = coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)["kline"]
            self.assertEqual((k["minutes"], k["rest_minutes"], k["complete_minutes"], k["lost_minutes"]), (715, 723, 1438, 2))
            self.assertAlmostEqual(k["pct"], 100.0 * 715 / 1440)          # socket only: not inflated by the history
            self.assertAlmostEqual(k["complete_pct"], 100.0 * 1438 / 1440)
            self.assertIn("REST", render(MK, SYM, coverage(root, MK, SYM, DAY, now_ms=D0 + 2 * DAY_MS)))
            prev = coverage(root, MK, SYM, "2026-10-17", now_ms=D0 + 2 * DAY_MS)  # only REST candles: the collector did not run
            self.assertFalse(prev["evidence"])
            self.assertEqual((prev["kline"]["pct"], prev["kline"]["rest_minutes"], prev["kline"]["complete_pct"]), (None, 720, 50.0))

    def test_meta_events_and_unrecoverable_ids(self):
        with tempfile.TemporaryDirectory() as root:
            t = D0 + H
            write(root, "meta", [[t, "connected"], [t + S, "disconnected", "no data for 30 s"], [t + 2 * S, "connected"],
                                 [t + 3 * S, "book_resync", 1], [t + 4 * S, "backfill_retry", 5, 9, 5, 1, "timed out"],
                                 [t + 5 * S, "trade_gap", 5, 9, "beyond BACKFILL_MAX"], [t + 6 * S, "trade_gap", 20, 20]])
            e = coverage(root, MK, SYM, DAY, now_ms=D0 + DAY_MS)["events"]
            self.assertEqual((e["connected"], e["disconnected"], e["book_resync"], e["backfill_retry"], e["trade_gap"]), (2, 1, 1, 1, 2))
            self.assertEqual((e["lost_ids"], e["gaps"]), (6, [[5, 9, "beyond BACKFILL_MAX"], [20, 20, ""]]))


if __name__ == "__main__":
    unittest.main()
