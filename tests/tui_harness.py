"""Child process for tests/test_tui.py — runs the real curses UI with fake
data and appends one line per frame (viewport state) to the log file given
as argv[1]. Not a test by itself.

argv[2] selects the scenario:
  scroll    (default) 50 static clients; exercises keys, mouse, columns
  watchdog  3-4 clients that come, go and burst; argv[3] = config file to
            write, argv[4] = watchdog event log (interval 1 s, alert 10 MB/s
            for 2 s)
"""
import curses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nfsmon  # noqa: E402

nfsmon.CONFIG_SYSTEM = nfsmon.CONFIG_USER = nfsmon.THEME_DIR = "/nonexistent/nfsmon-test"
mode = sys.argv[2] if len(sys.argv) > 2 else "scroll"


def static_connections():
    return [{"ip": f"10.0.0.{n}", "host": f"h-{n:02d}", "sent": n * 1000,
             "recv": n * 500, "conns": 1, "rtt_avg": 1.0} for n in range(1, 51)]


tick = {"n": 0}


def scripted_connections():
    # A bursts 20 MB/tick on ticks 1-5 (alert at tick 4 with duration 2 s,
    # cleared at tick 6). C is live on ticks 1-2, gone 3-4, back from 5
    # (disconnect, reconnect). D appears at tick 4 (connect).
    tick["n"] += 1
    t = tick["n"]
    a_sent = 20_000_000 * min(t, 5)
    rows = [{"ip": "10.0.0.1", "host": "a", "sent": a_sent, "recv": 0, "conns": 1, "rtt_avg": 1.0},
            {"ip": "10.0.0.2", "host": "b", "sent": 1000,   "recv": 0, "conns": 1, "rtt_avg": 1.0}]
    if t <= 2 or t >= 5:
        rows.append({"ip": "10.0.0.3", "host": "c", "sent": 500, "recv": 0, "conns": 1, "rtt_avg": 1.0})
    if t >= 4:
        rows.append({"ip": "10.0.0.4", "host": "d", "sent": 100, "recv": 0, "conns": 1, "rtt_avg": 1.0})
    return rows


if mode == "watchdog":
    cfg_path, events_path = sys.argv[3], sys.argv[4]
    with open(cfg_path, "w") as f:
        f.write("[general]\ninterval = 1\nwatchdog_enabled = true\n"
                f"[watchdog]\npath = {events_path}\n"
                "[alerts]\nthreshold_mb = 10\nduration_sec = 2\n")
    nfsmon.CONFIG_USER = cfg_path
    nfsmon.get_connections = scripted_connections
else:
    nfsmon.get_connections = static_connections

nfsmon.read_nfsd_clients_info = lambda need_states=True: {}
log = open(sys.argv[1], "w")
orig_draw = nfsmon.draw


def draw(stdscr, visible, *a, **kw):
    # positional after visible: total_count, sort_key, sort_rev, start_time,
    # show_help, filter_str, filter_input_active, group_subnet,
    # show_ip_in_host, selected_ip, ...
    log.write(f"scroll={kw.get('scroll_top')} sel={a[9]} group={a[7]} "
              f"n={len(visible)} rows={stdscr.getmaxyx()[0]} "
              f"col={kw.get('col_offset')} help={kw.get('help_scroll') if a[4] else '-'}\n")
    log.flush()
    return orig_draw(stdscr, visible, *a, **kw)


nfsmon.draw = draw
curses.wrapper(nfsmon.main)
