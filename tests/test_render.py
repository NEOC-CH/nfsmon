"""Pure helpers behind the rendering: totals, page size, DNS cache, health."""
import contextlib
import io
import os
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nfsmon  # noqa: E402


def row(ip, sent, recv, conns, **extra):
    base = {"ip": ip, "host": ip, "sent": sent, "recv": recv, "conns": conns,
            "age": 0, "dsent": 0, "drecv": 0, "rate_sent": 0.0, "rate_recv": 0.0}
    base.update(extra)
    return base


class TotalsTest(unittest.TestCase):
    def snapshot_footer(self, visible, filter_str=""):
        cols = nfsmon._build_active_cols({c[0]: True for c in nfsmon.COLUMNS}, False, 10)
        fd, p = tempfile.mkstemp()
        os.close(fd)
        try:
            nfsmon.write_snapshot(p, visible, 2, cols, "sent", True, filter_str,
                                  False, None, False, False)
            with open(p) as f:
                return f.read().rstrip().splitlines()[-1]
        finally:
            os.unlink(p)

    def test_ghost_rows_do_not_count(self):
        visible = [row("10.0.0.1", 1000, 100, 2), row("10.0.0.2", 2000, 200, 3),
                   row("10.0.0.9", 999999, 999999, 0, ghost=True)]
        foot = self.snapshot_footer(visible)
        self.assertTrue(foot.startswith("clients:2  total conns:5  sent:2.9 KB  recv:300.0 B"), foot)
        self.assertNotIn("999", foot)
        self.assertTrue(self.snapshot_footer(visible, "a").startswith("clients:2/2  conns:5"))

    def test_live_rows_helper(self):
        rows = [row("a", 1, 1, 1), row("b", 1, 1, 0, ghost=True)]
        self.assertEqual([r["ip"] for r in nfsmon._live_rows(rows)], ["a"])


class LayoutTest(unittest.TestCase):
    def test_data_rows_reserve_header_footer_and_ghost(self):
        rows_above = nfsmon.FIRST_DATA_ROW
        self.assertEqual(nfsmon.data_rows(12, False), 12 - rows_above - 1)
        self.assertEqual(nfsmon.data_rows(12, True), 12 - rows_above - 2)
        self.assertEqual(nfsmon.data_rows(3, True), 1)   # never below one row

    def test_popup_box_and_help_scroll_range(self):
        self.assertEqual(nfsmon.popup_box_dims(50, 120), (40, 70))
        self.assertEqual(nfsmon.popup_box_dims(12, 60), (10, 56))
        # 12 rows -> 10-row box -> 8 content rows
        self.assertEqual(nfsmon.help_max_scroll(12, 100), max(0, len(nfsmon.HELP_LINES) - 8))
        self.assertEqual(nfsmon.help_max_scroll(500, 100), max(0, len(nfsmon.HELP_LINES) - 38))

    def test_states_cell(self):
        self.assertEqual(nfsmon.states_str(None), "")
        self.assertEqual(nfsmon.states_str((0, 0, 0)), "")
        self.assertEqual(nfsmon.states_str((12, 3, 5)), "  12   3   5")

    def test_subnet_grouping_sums_states(self):
        rows = [row("10.0.10.1", 1, 1, 1, states=3, states_detail=(2, 1, 0)),
                row("10.0.10.2", 1, 1, 1, states=5, states_detail=(4, 0, 1)),
                row("10.0.20.9", 1, 1, 1, states=7, states_detail=(7, 0, 0))]
        groups = {g["ip"]: g for g in nfsmon.aggregate_by_subnet(rows)}
        self.assertEqual(groups["10.0.10.0/24"]["states"], 8)
        self.assertEqual(groups["10.0.10.0/24"]["states_detail"], (6, 1, 1))
        self.assertEqual(groups["10.0.20.0/24"]["states_detail"], (7, 0, 0))

    def test_snapshot_path_is_timestamped(self):
        p = nfsmon.snapshot_path(0)
        self.assertTrue(p.startswith(nfsmon.SNAPSHOT_DIR + "/nfsmon_snapshot_"), p)
        self.assertRegex(os.path.basename(p), r"^nfsmon_snapshot_\d{8}-\d{6}\.txt$")


class CliTest(unittest.TestCase):
    def test_overrides_parse_and_validate(self):
        a = nfsmon._parse_args(["--port", "20049", "--interval", "5", "--config", "/tmp/x.conf"])
        self.assertEqual((a.port, a.interval, a.config), (20049, 5, "/tmp/x.conf"))
        self.assertEqual(nfsmon._parse_args([]).port, None)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            nfsmon._parse_args(["--port", "70000"])


class DnsCacheTest(unittest.TestCase):
    def setUp(self):
        self._orig = socket.gethostbyaddr
        with nfsmon._dns_lock:
            nfsmon._dns_cache.clear()
            nfsmon._dns_pending.clear()

    def tearDown(self):
        socket.gethostbyaddr = self._orig

    def wait_resolved(self, ip):
        for _ in range(200):
            with nfsmon._dns_lock:
                if ip in nfsmon._dns_cache and ip not in nfsmon._dns_pending:
                    return
            time.sleep(0.01)
        self.fail("dns worker did not answer")

    def test_failure_is_retried_after_ttl(self):
        def fail(ip):
            raise socket.herror("no PTR")
        socket.gethostbyaddr = fail
        self.assertEqual(nfsmon.resolve_host("192.0.2.1"), "192.0.2.1")
        self.wait_resolved("192.0.2.1")
        self.assertEqual(nfsmon.resolve_host("192.0.2.1"), "192.0.2.1")   # cached failure
        with nfsmon._dns_lock:                                            # expire it
            nfsmon._dns_cache["192.0.2.1"] = ("192.0.2.1", time.time() - 1)
        socket.gethostbyaddr = lambda ip: ("host.example", [], [ip])
        nfsmon.resolve_host("192.0.2.1")                                  # triggers retry
        self.wait_resolved("192.0.2.1")
        self.assertEqual(nfsmon.resolve_host("192.0.2.1"), "host.example")

    def test_success_keeps_old_name_while_refreshing(self):
        socket.gethostbyaddr = lambda ip: ("old.example", [], [ip])
        nfsmon.resolve_host("192.0.2.2")
        self.wait_resolved("192.0.2.2")
        with nfsmon._dns_lock:
            nfsmon._dns_cache["192.0.2.2"] = ("old.example", time.time() - 1)
        socket.gethostbyaddr = lambda ip: ("new.example", [], [ip])
        self.assertEqual(nfsmon.resolve_host("192.0.2.2"), "old.example")  # stale, refresh queued
        self.wait_resolved("192.0.2.2")
        self.assertEqual(nfsmon.resolve_host("192.0.2.2"), "new.example")


class HealthTest(unittest.TestCase):
    def tearDown(self):
        nfsmon._health.clear()

    def test_set_and_clear(self):
        nfsmon._set_health("ss", "ss not found (install iproute2)")
        nfsmon._set_health("nfsd", "no access")
        self.assertEqual(nfsmon.health_warnings(), ["no access", "ss not found (install iproute2)"])
        nfsmon._set_health("ss", "")
        self.assertEqual(nfsmon.health_warnings(), ["no access"])

    def test_missing_ss_binary_sets_warning(self):
        orig = nfsmon.subprocess.run
        def missing(*a, **k):
            raise FileNotFoundError("ss")
        nfsmon.subprocess.run = missing
        try:
            self.assertEqual(nfsmon.get_connections(), [])
            self.assertIn("ss not found (install iproute2)", nfsmon.health_warnings())
        finally:
            nfsmon.subprocess.run = orig


if __name__ == "__main__":
    unittest.main()
