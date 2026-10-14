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

    def test_count_cell(self):
        self.assertEqual(nfsmon.count_str(None), "")
        self.assertEqual(nfsmon.count_str(0), "")
        self.assertEqual(nfsmon.count_str(12), "12")

    def test_subnet_grouping_sums_state_counters(self):
        rows = [row("192.0.2.1", 1, 1, 1, opens=2, locks=1, delegs=0),
                row("192.0.2.2", 1, 1, 1, opens=4, locks=0, delegs=1),
                row("198.51.100.9", 1, 1, 1, opens=7, locks=0, delegs=0)]
        groups = {g["ip"]: g for g in nfsmon.aggregate_by_subnet(rows)}
        a, b = groups["192.0.2.0/24"], groups["198.51.100.0/24"]
        self.assertEqual((a["opens"], a["locks"], a["delegs"]), (6, 1, 1))
        self.assertEqual((b["opens"], b["locks"], b["delegs"]), (7, 0, 0))
        for cid in ("opens", "locks", "delegs"):          # every sort key keeps working while grouped
            sorted(groups.values(), key=lambda x: x[cid])

    def test_snapshot_path_is_timestamped(self):
        p = nfsmon.snapshot_path(0)
        self.assertTrue(p.startswith(nfsmon.SNAPSHOT_DIR + "/nfsmon_snapshot_"), p)
        self.assertRegex(os.path.basename(p), r"^nfsmon_snapshot_\d{8}-\d{6}\.txt$")


class HotkeySchemeTest(unittest.TestCase):
    def test_sorting_is_shift_and_actions_are_not(self):
        # KEY_MAP: uppercase letters only (Shift+letter), each a real sort key
        for k, v in nfsmon.KEY_MAP.items():
            self.assertTrue(k.isupper() and len(k) == 1, k)
        self.assertEqual(set(nfsmon.KEY_MAP.values()) | {"activity", "spark"},
                         {c[0] for c in nfsmon.COLUMNS} | {"ip", "activity", "spark"})
        # help popup: every entry in the Sort section is Shift+letter (plus v),
        # no entry outside it uses Shift+letter except the vim G jump
        section = None
        for key, desc in nfsmon.HELP_LINES:
            if desc is None:
                section = key
                continue
            k = key.strip()
            if desc == "":
                continue
            if "Sort" in (section or ""):
                self.assertTrue(k == "v" or k.startswith("Shift+"), (k, desc))
            else:
                self.assertFalse(k.startswith("Shift+"), (k, desc))


class HostCellTest(unittest.TestCase):
    def test_dns_name_wins_then_client_name_then_ip(self):
        self.assertEqual(nfsmon.host_cell({"ip": "10.0.0.1", "host": "a.example", "client": "x"}), "a.example")
        self.assertEqual(nfsmon.host_cell({"ip": "10.0.0.1", "host": "10.0.0.1", "client": "client-01.example.com"}),
                         "~client-01.example.com")
        self.assertEqual(nfsmon.host_cell({"ip": "10.0.0.1", "host": "10.0.0.1", "client": ""}), "10.0.0.1")


class ServerStatsTest(unittest.TestCase):
    RPC = ("rc 0 0 562873286\nfh 2137 0 0 0 0\nio 1651606918399 3042621246437\n"
           "th 70 0 0.000 0.000\nrpc 562807003 23 23 0 0\nproc4 2 29997 562843289\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nfsmon-srv-")
        self._saved = (nfsmon.NFSD_THREADS_PATH, nfsmon.RPC_NFSD_PATH)
        nfsmon.NFSD_THREADS_PATH = os.path.join(self.tmp, "threads")
        nfsmon.RPC_NFSD_PATH = os.path.join(self.tmp, "nfsd")

    def tearDown(self):
        nfsmon.NFSD_THREADS_PATH, nfsmon.RPC_NFSD_PATH = self._saved

    def write(self, threads, rpc):
        with open(nfsmon.NFSD_THREADS_PATH, "w") as f:
            f.write(threads)
        with open(nfsmon.RPC_NFSD_PATH, "w") as f:
            f.write(rpc)

    def test_parse_real_format(self):
        self.write("70\n", self.RPC)
        self.assertEqual(nfsmon.read_server_stats(),
                         {"threads": 70, "stale_fh": 2137, "io_read": 1651606918399,
                          "io_write": 3042621246437, "rpc": 562807003, "bad_rpc": 23})

    def test_missing_files_give_empty_dict(self):
        self.assertEqual(nfsmon.read_server_stats(), {})
        self.assertEqual(nfsmon.server_stats_text({}), "")

    def test_rates_and_text(self):
        prev = {"threads": 70, "rpc": 1000, "io_read": 0, "io_write": 1_000_000, "bad_rpc": 5, "stale_fh": 9, "ts": 100.0}
        cur  = {"threads": 70, "rpc": 1400, "io_read": 0, "io_write": 2_600_000, "bad_rpc": 5, "stale_fh": 9}
        r = nfsmon.server_rates_from(prev, cur, 102.0)
        self.assertEqual(r["threads"], 70)
        self.assertAlmostEqual(r["rpc_s"], 200.0)
        self.assertAlmostEqual(r["write_s"], 800_000.0)
        self.assertEqual(nfsmon.server_stats_text(r), "nfsd:70thr  rpc:200/s  rd:0.0 B/s  wr:781.2 KB/s")
        self.assertFalse(nfsmon.server_stats_warn(r))
        cur2 = dict(cur, bad_rpc=7)
        r2 = nfsmon.server_rates_from(prev, cur2, 102.0)
        self.assertIn("badrpc:1/s", nfsmon.server_stats_text(r2))
        self.assertTrue(nfsmon.server_stats_warn(r2))

    def test_first_sample_and_counter_wrap(self):
        self.assertEqual(nfsmon.server_rates_from({}, {"threads": 8, "rpc": 5}, 1.0), {"threads": 8})
        r = nfsmon.server_rates_from({"rpc": 10, "ts": 0.0}, {"rpc": 3}, 1.0)   # wrapped → no rate
        self.assertNotIn("rpc_s", r)

    def test_fmt_rate(self):
        self.assertEqual([nfsmon.fmt_rate(x) for x in (0, 198.4, 1234, 2_500_000)], ["0", "198", "1.2k", "2.5M"])


class PruneSeenTest(unittest.TestCase):
    def test_drops_only_entries_older_than_cutoff(self):
        last = {"a": 100.0, "b": 500.0, "c": 900.0}
        snap = {"a": {}, "b": {}, "c": {}}
        self.assertEqual(nfsmon.prune_seen(last, snap, 600.0), 2)
        self.assertEqual(set(last), {"c"})
        self.assertEqual(set(snap), {"c"})
        self.assertEqual(nfsmon.prune_seen(last, snap, 600.0), 0)


class TextPopupTest(unittest.TestCase):
    def test_geometry_limits(self):
        text = "\n".join("x" * 200 for _ in range(60))
        g = nfsmon.text_popup_geometry(text, nfsmon.DETAIL_TITLE, 12, 100)
        self.assertEqual((g["box_h"], g["box_w"], g["page"]), (10, 98, 8))
        self.assertTrue(g["vbar"])
        self.assertEqual(g["text_w"], 95)                 # 96 inner minus the scrollbar column
        self.assertEqual((g["max_y"], g["max_x"]), (52, 105))
        small = nfsmon.text_popup_geometry("short\ntext", nfsmon.DETAIL_TITLE, 40, 120)
        self.assertEqual((small["max_y"], small["max_x"], small["vbar"]), (0, 0, False))


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
