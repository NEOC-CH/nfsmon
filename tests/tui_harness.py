"""Child process for tests/test_tui.py: runs the real curses UI with 50 fake
clients and appends one line per frame (viewport state) to the log file
given as argv[1]. Not a test by itself."""
import curses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nfsmon  # noqa: E402

nfsmon.CONFIG_SYSTEM = nfsmon.CONFIG_USER = nfsmon.THEME_DIR = "/nonexistent/nfsmon-test"


def fake_connections():
    return [{"ip": f"10.0.0.{n}", "host": f"h-{n:02d}", "sent": n * 1000,
             "recv": n * 500, "conns": 1, "rtt_avg": 1.0} for n in range(1, 51)]


nfsmon.get_connections = fake_connections
nfsmon.read_nfsd_clients_info = lambda need_mount=True: {}
log = open(sys.argv[1], "w")
orig_draw = nfsmon.draw


def draw(stdscr, visible, *a, **kw):
    # positional after visible: total_count, sort_key, sort_rev, start_time,
    # show_help, filter_str, filter_input_active, group_subnet,
    # show_ip_in_host, selected_ip, ...
    log.write(f"scroll={kw.get('scroll_top')} sel={a[9]} group={a[7]} "
              f"n={len(visible)} rows={stdscr.getmaxyx()[0]}\n")
    log.flush()
    return orig_draw(stdscr, visible, *a, **kw)


nfsmon.draw = draw
curses.wrapper(nfsmon.main)
