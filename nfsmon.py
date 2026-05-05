#!/usr/bin/env python3
# tools/nfsmon.py

import argparse
import configparser
import csv
import curses
import glob
import os
import subprocess
import socket
import sys
import threading
import time
import re
from queue import Queue
from typing import Dict, List, Optional, Set, Tuple

__version__ = "0.7.3"
__author__  = "NEOC CH"

INTERVAL = 2          # default tick interval in seconds; runtime-editable via `o`
PORT     = 2049       # NFS server port; runtime-editable via `o` (`global PORT` in main)

CSV_DEFAULT_PATH = "/var/log/nfsmon.csv"
CSV_HEADER = ["timestamp", "ip", "host", "sent", "recv",
              "dsent", "drecv", "rate_sent", "rate_recv", "conns"]

SNAPSHOT_PATH = "/tmp/nfsmon_snapshot.txt"
SNAPSHOT_TIMEOUT = 3.0   # seconds the footer flash-message stays visible

CONFIG_SYSTEM = "/etc/nfsmon.conf"                            # system-wide defaults
CONFIG_USER   = os.path.expanduser("~/.config/nfsmon.conf")   # per-user override + save target
THEME_DIR     = os.path.expanduser("~/.config/nfsmon/colors") # per-user theme files: <name>.conf

ALERT_DEFAULT_MB  = 10    # default alert threshold in MB/s
ALERT_DEFAULT_DUR = 5     # default alert duration in seconds (above threshold)

WATCHDOG_DEFAULT_PATH = "/var/log/nfsmon-events.log"
WATCHDOG_HEADER = ["timestamp", "event", "ip", "host", "last_sent", "last_recv"]

SPARK_LEN     = 10    # default TREND column width / rate-history samples
SPARK_MIN_LEN = 10    # minimum spark_len accepted from [defaults] tab
SPARK_MAX_LEN = 40    # maximum spark_len accepted from [defaults] tab


# ---------------------------------------------------------------------------
# Colors / theming
# ---------------------------------------------------------------------------
# Foreground color names accepted in [colors] / theme files. "default" maps to
# -1 (terminal default fg, requires use_default_colors()).
def _color_name_map() -> Dict[str, int]:
    # 8 base names work on every color terminal. The named aliases below
    # use 256-color codes — they only render correctly when curses.COLORS
    # is 256; on smaller terminals they get clamped down at init_pair time
    # in setup_colors() and silently fall back to the role's default fg.
    return {
        "default": -1,
        # 8 base palette
        "black":   curses.COLOR_BLACK,
        "red":     curses.COLOR_RED,
        "green":   curses.COLOR_GREEN,
        "yellow":  curses.COLOR_YELLOW,
        "blue":    curses.COLOR_BLUE,
        "magenta": curses.COLOR_MAGENTA,
        "cyan":    curses.COLOR_CYAN,
        "white":   curses.COLOR_WHITE,
        # 256-color aliases — see color.md for the full table
        "gray":         244,
        "darkgray":     240,
        "lightgray":    250,
        "silver":       145,
        "darkred":      88,
        "darkgreen":    22,
        "lightgreen":   120,
        "lime":         46,
        "mint":         121,
        "darkyellow":   136,
        "gold":         220,
        "orange":       208,
        "darkorange":   166,
        "peach":        216,
        "darkblue":     17,
        "lightblue":    117,
        "navy":         18,
        "teal":         30,
        "royalblue":    27,
        "darkcyan":     24,
        "lightcyan":    159,
        "purple":       91,
        "lightpurple":  141,
        "pink":         205,
        "hotpink":      199,
        "lightpink":    217,
        "brown":        94,
        "tan":          180,
    }


def _attr_name_map() -> Dict[str, int]:
    return {
        "bold":      curses.A_BOLD,
        "dim":       curses.A_DIM,
        "blink":     curses.A_BLINK,
        "reverse":   curses.A_REVERSE,
        "underline": curses.A_UNDERLINE,
        "standout":  curses.A_STANDOUT,
        "normal":    curses.A_NORMAL,
    }


# role_name -> (fg_color_name, "comma,separated,attrs"). These are the
# built-in defaults; [colors] in config and theme files override per role.
DEFAULT_COLORS: Dict[str, Tuple[str, str]] = {
    "title":           ("cyan",   "bold"),
    "key_bar":         ("yellow", "bold"),
    "text":            ("green",  ""),
    "footer":          ("cyan",   ""),
    "alert":           ("red",    "bold,blink"),
    "idle":            ("white",  "dim"),
    "activity_low":    ("green",  ""),
    "activity_medium": ("yellow", "bold"),
    "activity_high":   ("red",    "bold"),
}

# Populated by setup_colors(): role_name -> curses attr (color_pair | flags).
# attr_for(role) reads this; do not access directly elsewhere.
_ROLE_ATTR: Dict[str, int] = {}


def _open_csv(path: str):
    # Open in append mode; write header only if the file is new (or empty).
    # Returns (file, writer) — caller is responsible for closing on toggle-off.
    new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(CSV_HEADER)
        f.flush()
    return f, w


def _open_watchdog(path: str):
    # Same shape as _open_csv but for the watchdog event log. One row per
    # detected disconnect; header written on file creation. Append-only.
    new = (not os.path.exists(path)) or os.path.getsize(path) == 0
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(WATCHDOG_HEADER)
        f.flush()
    return f, w


def _parse_color_spec(spec: str) -> Tuple[str, str]:
    # "cyan,bold,blink" -> ("cyan", "bold,blink"). First token is fg color name,
    # rest are attribute names (bold/dim/blink/reverse/underline/standout/normal).
    parts = [t.strip().lower() for t in (spec or "").split(",") if t.strip()]
    if not parts:
        return ("default", "")
    return (parts[0], ",".join(parts[1:]))


def _format_color_spec(fg: str, attrs: str) -> str:
    a = (attrs or "").strip()
    return f"{fg},{a}" if a else fg


def list_themes() -> List[str]:
    # Theme names are filenames in THEME_DIR without the .conf suffix.
    if not os.path.isdir(THEME_DIR):
        return []
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(THEME_DIR, "*.conf"))
    )


def load_theme(theme_name: str) -> Dict[str, Tuple[str, str]]:
    # Returns role -> (fg_name, attrs) overrides from the theme's [colors]
    # section. Empty dict if theme is unset / file missing / no [colors].
    if not theme_name:
        return {}
    path = os.path.join(THEME_DIR, f"{theme_name}.conf")
    if not os.path.isfile(path):
        return {}
    cp = configparser.ConfigParser()
    try:
        cp.read([path])
    except configparser.Error:
        return {}
    if not cp.has_section("colors"):
        return {}
    out: Dict[str, Tuple[str, str]] = {}
    for role in DEFAULT_COLORS:
        if cp.has_option("colors", role):
            out[role] = _parse_color_spec(cp.get("colors", role))
    return out


def load_config() -> Dict:
    # Read CONFIG_SYSTEM first (system defaults) then CONFIG_USER (user
    # override). configparser merges in read-order — later files win on
    # duplicate keys. Missing files are silently ignored. Returns a flat
    # dict; missing keys are absent so callers fall back to built-in defaults.
    cp = configparser.ConfigParser()
    try:
        cp.read([CONFIG_SYSTEM, CONFIG_USER])
    except configparser.Error:
        return {}
    out: Dict = {}

    def _opt(section: str, option: str, kind: str):
        if not cp.has_option(section, option):
            return None
        try:
            if kind == "int":  return cp.getint(section, option)
            if kind == "bool": return cp.getboolean(section, option)
            return cp.get(section, option)
        except (ValueError, configparser.Error):
            return None

    for key, kind in (("interval", "int"), ("port", "int"),
                      ("group_subnet", "bool"), ("show_ip", "bool"),
                      ("sort_key", "str"), ("sort_rev", "bool"),
                      ("theme", "str"),
                      ("bright_sort_col", "bool"),
                      ("show_seen_clients", "bool"),
                      ("watchdog_enabled", "bool"),
                      ("spark_len", "int")):
        v = _opt("general", key, kind)
        if v is not None:
            out[key] = v

    for key, kind in (("enabled", "bool"), ("path", "str")):
        v = _opt("csv", key, kind)
        if v is not None:
            out["csv_" + key] = v

    for key, kind in (("path", "str"),):
        v = _opt("watchdog", key, kind)
        if v is not None:
            out["watchdog_" + key] = v

    if cp.has_section("columns"):
        cv: Dict[str, bool] = {}
        for col_id, *_ in COLUMNS:
            v = _opt("columns", col_id, "bool")
            if v is not None:
                cv[col_id] = v
        if cv:
            out["cols_visible"] = cv

    for key, kind in (("threshold_mb", "int"), ("duration_sec", "int")):
        v = _opt("alerts", key, kind)
        if v is not None:
            out["alert_" + ("mb" if key == "threshold_mb" else "dur")] = v

    if cp.has_section("colors"):
        co: Dict[str, Tuple[str, str]] = {}
        for role in DEFAULT_COLORS:
            if cp.has_option("colors", role):
                co[role] = _parse_color_spec(cp.get("colors", role))
        if co:
            out["colors"] = co

    return out


def save_config(interval: int, port: int, group_subnet: bool,
                show_ip_in_host: bool,
                sort_key: str, sort_rev: bool,
                csv_enabled: bool, csv_path: str,
                cols_visible: Dict[str, bool],
                alert_mb: int, alert_dur: int,
                theme: str = "",
                colors: Optional[Dict[str, Tuple[str, str]]] = None,
                bright_sort_col: bool = False,
                show_seen_clients: bool = False,
                watchdog_enabled: bool = False,
                watchdog_path: str = WATCHDOG_DEFAULT_PATH,
                spark_len: int = SPARK_LEN) -> bool:
    # Always writes to CONFIG_USER. System config CONFIG_SYSTEM stays
    # untouched. Creates the parent dir if needed. Returns True on success,
    # False on OSError. theme is a theme filename (without .conf) or "" for
    # none. colors is the role -> (fg, attrs) overlay; if None, omitted.
    cp = configparser.ConfigParser()
    cp["general"] = {
        "interval":          str(interval),
        "port":              str(port),
        "group_subnet":      "true" if group_subnet else "false",
        "show_ip":           "true" if show_ip_in_host else "false",
        "sort_key":          sort_key,
        "sort_rev":          "true" if sort_rev else "false",
        "theme":             theme or "",
        "bright_sort_col":   "true" if bright_sort_col else "false",
        "show_seen_clients": "true" if show_seen_clients else "false",
        "watchdog_enabled":  "true" if watchdog_enabled else "false",
        "spark_len":         str(spark_len),
    }
    cp["csv"] = {
        "enabled": "true" if csv_enabled else "false",
        "path":    csv_path,
    }
    cp["watchdog"] = {
        "path": watchdog_path,
    }
    cp["columns"] = {col_id: ("true" if cols_visible.get(col_id, True) else "false")
                     for col_id, *_ in COLUMNS}
    cp["alerts"] = {
        "threshold_mb": str(alert_mb),
        "duration_sec": str(alert_dur),
    }
    if colors:
        cp["colors"] = {role: _format_color_spec(fg, attrs)
                        for role, (fg, attrs) in colors.items()}
    try:
        os.makedirs(os.path.dirname(CONFIG_USER), exist_ok=True)
        with open(CONFIG_USER, "w") as f:
            cp.write(f)
        return True
    except OSError:
        return False


# --- async DNS resolution ---------------------------------------------------
# gethostbyaddr can stall multiple seconds on first lookup; doing it on the
# UI thread froze input. Resolve in a daemon worker; callers get the IP
# back immediately and the resolved name appears on a later redraw.
_dns_cache:   Dict[str, str] = {}
_dns_pending: Set[str]       = set()
_dns_lock                    = threading.Lock()
_dns_queue:   "Queue[str]"   = Queue()


def _dns_worker() -> None:
    while True:
        ip = _dns_queue.get()
        try:
            name = socket.gethostbyaddr(ip)[0]
        except Exception:
            name = ip
        with _dns_lock:
            _dns_cache[ip] = name
            _dns_pending.discard(ip)


threading.Thread(target=_dns_worker, daemon=True).start()


def resolve_host(ip: str) -> str:
    with _dns_lock:
        if ip in _dns_cache:
            return _dns_cache[ip]
        if ip in _dns_pending:
            return ip
        _dns_pending.add(ip)
    _dns_queue.put(ip)
    return ip


def bytes_human(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# --- sparkline -------------------------------------------------------------
# Tiered mapping aligned with the activity-color scheme so the same bar
# height means the same bandwidth tier across all rows. SPARK_LEN /
# SPARK_MIN_LEN / SPARK_MAX_LEN live near the top of the file so the
# default-arg `spark_len=SPARK_LEN` on save_config / draw / etc. resolves.
SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _spark_char(rate_bps: float) -> str:
    if rate_bps <= 0:                       return " "
    if rate_bps < 100 * 1024:               return SPARK_CHARS[0]
    if rate_bps < 500 * 1024:               return SPARK_CHARS[1]
    if rate_bps < 1024 * 1024:              return SPARK_CHARS[2]
    if rate_bps < 5  * 1024 * 1024:         return SPARK_CHARS[3]
    if rate_bps < 10 * 1024 * 1024:         return SPARK_CHARS[4]
    if rate_bps < 50 * 1024 * 1024:         return SPARK_CHARS[5]
    if rate_bps < 100 * 1024 * 1024:        return SPARK_CHARS[6]
    return SPARK_CHARS[7]


def sparkline(values: List[float], length: int = SPARK_LEN) -> str:
    # Render at most `length` recent samples; left-pad with spaces if the
    # history hasn't filled up yet. Length is the runtime spark_len from
    # the [defaults] tab, or the SPARK_LEN default when not threaded.
    if not values:
        return " " * length
    recent = values[-length:]
    pad    = length - len(recent)
    return (" " * pad) + "".join(_spark_char(v) for v in recent)


def get_connections() -> List[Dict]:
    try:
        out = subprocess.run(
            ["ss", "-tniH", "state", "established", "( sport = :{} )".format(PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        ).stdout.decode("utf-8", errors="replace")
    except Exception:
        return []

    # aggregate per ip -> {sent, recv, conns, rtt_sum, rtt_count}
    agg        = {}
    current_ip = None

    for line in out.splitlines():
        if re.match(r"^\d", line):                       # connection line
            parts = line.split()
            if len(parts) >= 4:
                current_ip = re.sub(r":\d+$", "", parts[3])

        elif "bytes_" in line and current_ip:            # tcp-info line
            sent = recv = 0
            rtt  = None
            # Prefer bytes_acked (data peer has actually ACKed) and fall back
            # to bytes_sent only if not present. Newer kernels emit both;
            # picking the leftmost non-deterministically mixed semantics.
            m = re.search(r"bytes_acked:(\d+)", line)
            if not m:
                m = re.search(r"bytes_sent:(\d+)", line)
            if m:
                sent = int(m.group(1))
            m = re.search(r"bytes_received:(\d+)", line)
            if m:
                recv = int(m.group(1))
            # rtt:X.X/Y.Y — first value is smoothed RTT in ms, second is
            # mean deviation. Some kernels/states omit the line entirely.
            m = re.search(r"\brtt:(\d+(?:\.\d+)?)", line)
            if m:
                rtt = float(m.group(1))
            if current_ip not in agg:
                agg[current_ip] = {"sent": 0, "recv": 0, "conns": 0,
                                   "rtt_sum": 0.0, "rtt_count": 0}
            agg[current_ip]["sent"]  += sent
            agg[current_ip]["recv"]  += recv
            agg[current_ip]["conns"] += 1
            if rtt is not None:
                agg[current_ip]["rtt_sum"]   += rtt
                agg[current_ip]["rtt_count"] += 1
            current_ip = None

    return [
        {
            "ip":      ip,
            "host":    resolve_host(ip),
            "sent":    v["sent"],
            "recv":    v["recv"],
            "conns":   v["conns"],
            "rtt_avg": (v["rtt_sum"] / v["rtt_count"]) if v["rtt_count"] else 0.0,
        }
        for ip, v in agg.items()
    ]


def _common_dir_prefix(paths: List[str]) -> str:
    # Longest path-component-aligned common prefix of absolute paths. With
    # exactly one input path the basename is dropped so the result is the
    # containing directory (a single "open file" still gives a useful mount
    # hint instead of the file itself).
    if not paths:
        return ""
    parts_sets = [p.split("/") for p in paths]
    min_len    = min(len(p) for p in parts_sets)
    common: List[str] = []
    for i in range(min_len):
        first = parts_sets[0][i]
        if all(p[i] == first for p in parts_sets):
            common.append(first)
        else:
            break
    if len(paths) == 1 and len(common) == len(parts_sets[0]):
        common.pop()
    joined = "/".join(common)
    return joined if joined else ""


def read_nfsd_clients_info() -> Dict[str, Dict[str, str]]:
    # Parse /proc/fs/nfsd/clients/<id>/{info,states} per peer. Cheap small
    # reads (~30 files even on a busy server). Returns ip -> {"nfsv": "4.2",
    # "mount": "/exports/nfs-home"}. mount is approximated as the longest
    # common dir prefix of currently open file paths in `states` — empty if
    # the client has no open state, or if the kernel doesn't expose states.
    result: Dict[str, Dict[str, str]] = {}
    base = "/proc/fs/nfsd/clients"
    if not os.path.isdir(base):
        return result
    try:
        cids = os.listdir(base)
    except OSError:
        return result
    for cid in cids:
        info_path   = os.path.join(base, cid, "info")
        states_path = os.path.join(base, cid, "states")
        try:
            with open(info_path) as f:
                info = f.read()
        except OSError:
            continue
        ip      = None
        version = None
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("address:"):
                m = re.search(r'"([^"]+)"', line)
                if m:
                    # strip trailing :port (works for both IPv4 "1.2.3.4:0"
                    # and bracketed IPv6 "[::1]:0")
                    ip = re.sub(r":\d+$", "", m.group(1))
            elif line.startswith("minor version:"):
                v = line.split(":", 1)[1].strip()
                if v.isdigit():
                    version = f"4.{v}"
        # mount: longest common dir prefix across all quoted absolute paths
        # in the states file. Format varies a bit per kernel — we just pull
        # any "/..."-style quoted strings rather than parsing the structure.
        mount = ""
        try:
            with open(states_path) as f:
                states = f.read()
            paths = re.findall(r'"(/[^"]+)"', states)
            mount = _common_dir_prefix(paths)
        except OSError:
            pass
        if ip:
            result[ip] = {"nfsv": version or "", "mount": mount}
    return result


# ---------- curses UI -------------------------------------------------------

SORT_CYCLE = ["sent", "recv", "dsent", "drecv", "rate_sent", "rate_recv",
              "activity", "conns", "age", "ip", "host"]
KEY_MAP     = {"s": "sent",      "r": "recv",
               "S": "dsent",     "R": "drecv",
               "b": "rate_sent", "B": "rate_recv",
               "c": "conns",     "a": "age",
               "i": "ip",        "H": "host",
               "n": "nfsv",      "m": "mount",
               "t": "rtt_avg",   "l": "last_seen"}


def elapsed_str(since: float) -> str:
    s = int(time.time() - since)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "{:d}d {:02d}:{:02d}:{:02d}".format(d, h, m, s)
    return "{:02d}:{:02d}:{:02d}".format(h, m, s)


def last_seen_str(ts: float) -> str:
    # Compact "how long ago" used for the SEEN column. ts == 0 → blank,
    # so live conns that haven't been seen yet (shouldn't happen) and
    # subnet-aggregate rows render empty rather than "0s ago".
    if not ts:
        return ""
    diff = int(time.time() - ts)
    if diff < 2:                  return "now"
    if diff < 60:                 return f"{diff}s"
    if diff < 3600:
        m, s = divmod(diff, 60)
        return f"{m}m {s}s" if m < 10 else f"{m}m"
    if diff < 86400:
        h, rem = divmod(diff, 3600)
        m, _   = divmod(rem,  60)
        return f"{h}h {m}m" if h < 10 else f"{h}h"
    d, rem = divmod(diff, 86400)
    h, _   = divmod(rem,  3600)
    return f"{d}d {h}h" if d < 10 else f"{d}d"


# Column definitions drive the table header and data rows. Each entry is
# (id, label, width, align, value_fn). cols_visible[id] gates whether the
# column is rendered; the order in COLUMNS is the on-screen order.
COLUMNS = [
    ("host",      "HOST",      30, "<", lambda c: c["host"]),
    ("nfsv",      "NFSv",       4, "<", lambda c: c.get("nfsv", "")),
    ("mount",     "MOUNT",     24, "<", lambda c: c.get("mount", "")),
    ("rtt_avg",   "RTT",        9, ">", lambda c: (f"{c['rtt_avg']:.1f} ms" if c.get("rtt_avg", 0) > 0 else "")),
    ("conns",     "CONNS",      6, ">", lambda c: str(c["conns"])),
    ("age",       "CONNECTED", 11, ">", lambda c: elapsed_str(c["age"])),
    ("last_seen", "SEEN",      10, ">", lambda c: last_seen_str(c.get("last_seen", 0))),
    ("sent",      "SENT",      12, ">", lambda c: bytes_human(c["sent"])),
    ("dsent",     "ΔSENT",     12, ">", lambda c: bytes_human(c["dsent"])),
    ("rate_sent", "B/s SENT",  12, ">", lambda c: bytes_human(int(c.get("rate_sent", 0))) + "/s"),
    ("recv",      "RECV",      12, ">", lambda c: bytes_human(c["recv"])),
    ("drecv",     "ΔRECV",     12, ">", lambda c: bytes_human(c["drecv"])),
    ("rate_recv", "B/s RECV",  12, ">", lambda c: bytes_human(int(c.get("rate_recv", 0))) + "/s"),
    ("spark",     "TREND",     SPARK_LEN, "<", lambda c: c.get("spark", "")),
]


def _build_active_cols(cols_visible: Dict[str, bool], show_ip_in_host: bool,
                       spark_len: int = SPARK_LEN):
    # Active column list: filter by visibility, then transform the HOST
    # entry to render the raw IP when show_ip_in_host is True (label "IP",
    # lambda returns c["ip"]), and substitute the runtime `spark_len` for
    # the TREND column's static width. Single point of truth used by both
    # the renderer and write_snapshot.
    out = []
    for col in COLUMNS:
        if not cols_visible.get(col[0], True):
            continue
        if col[0] == "host" and show_ip_in_host:
            out.append(("host", "IP", col[2], col[3], lambda c: c["ip"]))
        elif col[0] == "spark":
            out.append(("spark", "TREND", spark_len, "<", lambda c: c.get("spark", "")))
        else:
            out.append(col)
    return out


# Tabs in the Options popup. Each tab holds uniform item kinds (no headers
# or spacers); Tab/Shift-Tab cycles tabs, ↑/↓ navigates within the active
# tab. New tabs ([export], [alerts], ...) get added when those features land.
TABS = [
    ("defaults", "defaults", [
        ("interval",  "interval",         "Interval (s)"),
        ("port",      "port",             "Port"),
        ("csv",       "csv",              "CSV Log"),
        ("alert_mb",  "alert_mb",         "Alert MB/s"),
        ("alert_dur", "alert_dur",        "Alert duration (s)"),
        ("theme",     "theme",            "Theme"),
        ("bright",    "bright_sort_col",  "Bright sorted column"),
        ("seen",      "show_seen_clients", "Show last-seen clients"),
        ("watchdog",  "watchdog_enabled", "Watchdog (log disconnects)"),
        ("spark_len", "spark_len",        "TREND length"),
    ]),
    ("columns",  "columns",
        [("show", c[0], c[1]) for c in COLUMNS]),
    ("sort",     "sort",
        [("sort", c[0], c[1]) for c in COLUMNS] +
        [("sort", "ip",       "IP"),
         ("sort", "activity", "ACTIVITY (idle first w/ v)")]),
]


HELP_LINES = [
    ("─── Sort ─────────────────────────────────", None),
    ("  s",         "Sort by SENT (total)"),
    ("  Shift+s",   "Sort by ΔSENT (delta)"),
    ("  r",         "Sort by RECV (total)"),
    ("  Shift+r",   "Sort by ΔRECV (delta)"),
    ("  b",         "Sort by B/s SENT (live rate)"),
    ("  Shift+b",   "Sort by B/s RECV (live rate)"),
    ("  c",         "Sort by CONNS"),
    ("  a",         "Sort by CONNECTED (age)"),
    ("  Shift+a",   "Sort by ACTIVITY (idle first)"),
    ("  i",         "Sort by IP"),
    ("  Shift+h",   "Sort by HOST (hostname)"),
    ("  n",         "Sort by NFSv"),
    ("  m",         "Sort by MOUNT"),
    ("  t",         "Sort by RTT (latency)"),
    ("  l",         "Sort by SEEN (last seen)"),
    ("  v",         "Toggle sort direction ↑↓"),
    ("─── View ─────────────────────────────────", None),
    ("  /",         "Filter by host or IP (substring)"),
    ("  ↑/↓",       "Select row"),
    ("  PgUp/PgDn", "Select ±5 rows"),
    ("  Enter",     "Show detail (selected row)"),
    ("  f",         "Follow selection (pin / unpin)"),
    ("  g",         "Group by /24 subnet"),
    ("  Shift+i",   "Toggle host column: hostname ↔ IP"),
    ("─── Actions ──────────────────────────────", None),
    ("  o",         "Open Options popup"),
    ("  Shift+d",   f"Snapshot dump → {SNAPSHOT_PATH}"),
    ("  space",     "Pause / resume display"),
    ("  z",         "Reset baseline (zero Δ + age + since)"),
    ("─── General ──────────────────────────────", None),
    ("  h",         "Toggle this help"),
    ("  q",         "Quit"),
    ("  Esc",       "Close popup / cancel input"),
    ("──────────────────────────────────────────", None),
    ("TREND has no sort key (sparkline is not comparable);", ""),
    ("use the [sort] tab in the Options popup if needed.",  ""),
    ("ΔSENT/ΔRECV = bytes since baseline (z resets).",      ""),
]


def safe_addstr(stdscr, row: int, col: int, text: str, attr: int = 0) -> None:
    # Writing to the bottom-right cell or past terminal width raises
    # curses.error. Swallow it — partial draws are fine, crashes aren't.
    try:
        stdscr.addstr(row, col, text, attr)
    except curses.error:
        pass


def draw_help_popup(stdscr) -> None:
    height, width = stdscr.getmaxyx()
    # Same fixed footprint as the Options popup (70×40, clamped to terminal)
    # so help and options have a consistent visual size and don't make the
    # screen jump when switching between them.
    box_h = min(40, max(8,  height - 2))
    box_w = min(70, max(20, width  - 4))
    row0  = max(0, (height - box_h) // 2)
    col0  = max(0, (width  - box_w) // 2)
    inner = box_w - 2
    if inner < 4:
        return

    C_BOX  = attr_for("title")
    C_KEY  = attr_for("key_bar")
    C_TEXT = attr_for("text")
    C_HDR  = attr_for("footer") | curses.A_BOLD

    # top border with embedded title
    title = " NFS Monitor  ─  Keybindings "
    if inner >= len(title):
        pad   = inner - len(title)
        ldash = pad // 2
        rdash = pad - ldash
        top   = "┌" + "─" * ldash + title + "─" * rdash + "┐"
    else:
        top = "┌" + title[:inner] + "┐"
    safe_addstr(stdscr, row0, col0, top, C_BOX)

    # paint side borders + blank fill across all inner rows up front;
    # content then overlays on top.
    for r in range(row0 + 1, row0 + box_h - 1):
        if r >= height:
            break
        safe_addstr(stdscr, r, col0, "│" + " " * inner + "│", C_BOX)

    # content rows (HELP_LINES) — overlay on the painted background
    last_content = row0 + box_h - 2   # last writable row above bottom border
    for idx, (left, right) in enumerate(HELP_LINES):
        r = row0 + 1 + idx
        if r > last_content or r >= height:
            break
        if right is None:                              # section header
            safe_addstr(stdscr, r, col0 + 1,
                        (" " + left).ljust(inner)[:inner], C_HDR)
        elif right == "":                              # note line
            safe_addstr(stdscr, r, col0 + 1,
                        ("  " + left).ljust(inner)[:inner], C_TEXT)
        else:
            safe_addstr(stdscr, r, col0 + 1, left[:inner], C_KEY)
            # bumped from col0+11 → col0+16 so "Shift+x" / "PgUp/PgDn" fit
            desc_col = col0 + 16
            desc_max = (col0 + box_w - 1) - desc_col
            if desc_max > 0:
                safe_addstr(stdscr, r, desc_col, right[:desc_max], C_TEXT)

    # bottom border
    bottom_row = row0 + box_h - 1
    if bottom_row < height:
        safe_addstr(stdscr, bottom_row, col0,
                    "└" + "─" * inner + "┘", C_BOX)


def draw_columns_popup(stdscr, cols_visible: Dict[str, bool],
                       sort_key: str, sel_idx: int,
                       active_tab_idx: int,
                       focus_zone: str,
                       interval: int,
                       edit_field: Optional[str],
                       edit_input: str,
                       csv_enabled: bool,
                       csv_path: str,
                       alert_mb: int,
                       alert_dur: int,
                       current_theme: str = "",
                       bright_sort_col: bool = False,
                       show_seen_clients: bool = False,
                       watchdog_enabled: bool = False,
                       watchdog_path: str = "",
                       spark_len: int = SPARK_LEN) -> None:
    height, width = stdscr.getmaxyx()
    # Fixed target size (70×40, W×H) clamped to terminal so the popup never
    # changes size between tabs. Width was bumped from 60 to 70 to fit the
    # [Save Settings] button at the bottom-right next to the hint text.
    box_h = min(40, max(8,  height - 2))
    box_w = min(70, max(20, width  - 4))
    row0  = max(0, (height - box_h) // 2)
    col0  = max(0, (width  - box_w) // 2)
    inner = box_w - 2
    if inner < 4:
        return

    C_BOX          = attr_for("title")
    C_KEY          = attr_for("key_bar")
    C_TEXT         = attr_for("text")
    C_TAB_ACTIVE   = attr_for("key_bar") | curses.A_REVERSE
    C_TAB_INACTIVE = attr_for("text") | curses.A_DIM

    # focus_zone is either "main" (tab bar + body items active) or "save"
    # (the [Save Settings] button at the bottom-right has focus). active_tab_idx
    # is always a real tab index — Tab toggles between zones, ←/→ in main
    # switches active_tab_idx.
    save_focused = focus_zone == "save"
    if save_focused:
        sel_idx = -1   # suppress per-item highlight while save has focus

    # top border with embedded title
    title = " Options "
    if inner >= len(title):
        pad   = inner - len(title)
        ldash = pad // 2
        rdash = pad - ldash
        top   = "┌" + "─" * ldash + title + "─" * rdash + "┐"
    else:
        top = "┌" + title[:inner] + "┐"
    safe_addstr(stdscr, row0, col0, top, C_BOX)

    # paint side borders + blank fill across all inner rows up front;
    # tab bar / content / hint then overlay on top.
    for r in range(row0 + 1, row0 + box_h - 1):
        if r >= height:
            break
        safe_addstr(stdscr, r, col0, "│" + " " * inner + "│", C_BOX)

    # tab bar (first inner row) — active_tab_idx is highlighted; ←/→ in main
    # zone switches it. In save zone the active tab stays highlighted so the
    # user keeps orientation about which tab's settings would be saved.
    tab_row = row0 + 1
    if tab_row < height:
        tab_x = col0 + 2
        for i, (_tid, label, _items) in enumerate(TABS):
            text = f"[{label}]"
            if tab_x + len(text) >= col0 + box_w - 1:
                break
            attr = C_TAB_ACTIVE if i == active_tab_idx else C_TAB_INACTIVE
            safe_addstr(stdscr, tab_row, tab_x, text, attr)
            tab_x += len(text) + 2

    # content area: items from the active tab (when save_focused, sel_idx is
    # -1 above so no item gets the reverse highlight).
    items         = TABS[active_tab_idx][2]
    content_start = row0 + 3
    last_content  = row0 + box_h - 3   # leaves one row for hint above bottom

    for idx, item in enumerate(items):
        r = content_start + idx
        if r > last_content or r >= height:
            break
        kind = item[0]
        if kind == "show":
            col_id, label = item[1], item[2]
            mark = "[x]" if cols_visible.get(col_id, True) else "[ ]"
            text = f" {mark} {label}".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "sort":
            col_id, label = item[1], item[2]
            mark = "(•)" if sort_key == col_id else "( )"
            text = f" {mark} {label}".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "interval":
            label = item[2]
            if edit_field == "interval" and idx == sel_idx:
                val_str = (edit_input + "_")[:5]   # incl. cursor
            else:
                val_str = str(interval)
            text = f" {label}:  [{val_str:>4}]".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "port":
            label = item[2]
            if edit_field == "port" and idx == sel_idx:
                val_str = (edit_input + "_")[:6]   # 5-digit + cursor
            else:
                val_str = str(PORT)
            text = f" {label}:  [{val_str:>5}]".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "csv":
            label = item[2]
            mark  = "[x]" if csv_enabled else "[ ]"
            text  = f" {label}:  {mark}  {csv_path}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "alert_mb":
            label = item[2]
            if edit_field == "alert_mb" and idx == sel_idx:
                val_str = (edit_input + "_")[:5]
            else:
                val_str = str(alert_mb)
            text = f" {label}:  [{val_str:>4}]".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "alert_dur":
            label = item[2]
            if edit_field == "alert_dur" and idx == sel_idx:
                val_str = (edit_input + "_")[:5]
            else:
                val_str = str(alert_dur)
            text = f" {label}:  [{val_str:>4}]".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "theme":
            label    = item[2]
            shown    = current_theme if current_theme else "(default)"
            text     = f" {label}:  ‹ {shown} ›  (space to cycle)".ljust(inner)[:inner]
            attr     = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "bright":
            label = item[2]
            mark  = "[x]" if bright_sort_col else "[ ]"
            text  = f" {label}:  {mark}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "seen":
            label = item[2]
            mark  = "[x]" if show_seen_clients else "[ ]"
            text  = f" {label}:  {mark}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "watchdog":
            label = item[2]
            mark  = "[x]" if watchdog_enabled else "[ ]"
            text  = f" {label}:  {mark}  {watchdog_path}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "spark_len":
            label = item[2]
            if edit_field == "spark_len" and idx == sel_idx:
                val_str = (edit_input + "_")[:3]   # 2-digit + cursor
            else:
                val_str = str(spark_len)
            text = f" {label}:  [{val_str:>2}]  ({SPARK_MIN_LEN}-{SPARK_MAX_LEN})".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)

    # hint row (one above bottom border) — left-side hint + [Save Settings]
    # button right-aligned. Button is reverse-highlighted when save_focused,
    # dim otherwise. Reachable via Tab/⇧Tab from any real tab.
    hint_row = row0 + box_h - 2
    if hint_row < height:
        if edit_field is not None:
            hint = " 0-9 ⌫  Enter:apply  Esc:cancel "
        else:
            hint = " Tab:save  ←→:tabs  ↑↓:sel  space:toggle  Esc:close "
        save_label = "[Save Settings]"
        save_attr  = (attr_for("key_bar") | curses.A_REVERSE
                      if save_focused
                      else attr_for("text") | curses.A_DIM)
        hint_w = max(0, inner - len(save_label) - 1)
        safe_addstr(stdscr, hint_row, col0 + 1,
                    hint.ljust(hint_w)[:hint_w], C_KEY)
        save_x = col0 + box_w - 1 - len(save_label)
        if save_x > col0 + 1:
            safe_addstr(stdscr, hint_row, save_x, save_label, save_attr)

    # bottom border
    bottom_row = row0 + box_h - 1
    if bottom_row < height:
        safe_addstr(stdscr, bottom_row, col0,
                    "└" + "─" * inner + "┘", C_BOX)


def compute_visible(connections: List[Dict], filter_str: str, group_subnet: bool,
                    sort_key: str, sort_rev: bool) -> List[Dict]:
    # Single source of truth for what's currently displayed: filter, then
    # optionally aggregate, then sort. Used by both the renderer and the
    # arrow-key navigation in main().
    if filter_str:
        f = filter_str.lower()
        connections = [c for c in connections
                       if f in c["host"].lower() or f in c["ip"]]
    if group_subnet:
        connections = aggregate_by_subnet(connections)
    return sorted(connections, key=lambda x: x[sort_key], reverse=sort_rev)


def fetch_detail(ip: str) -> str:
    # Snapshot of the peer's TCP info + (if available) NFS-server-side client
    # info from procfs. Captured at the moment the user opens the popup.
    out_lines: List[str] = []

    try:
        result = subprocess.run(
            ["ss", "-tinH", "state", "established",
             "( sport = :{} )".format(PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        ss_text = result.stdout.decode("utf-8", errors="replace")
        ip_re   = re.compile(r"(?:^|[^\d.])" + re.escape(ip) + r":")
        matched: List[str] = []
        keep    = False
        for ln in ss_text.splitlines():
            if not ln.strip():
                continue
            if re.match(r"^\d", ln):           # new connection summary
                keep = bool(ip_re.search(ln))
                if keep:
                    matched.append(ln)
            elif keep:                         # tcp-info continuation
                matched.append(ln)
        if matched:
            out_lines.append(f"── ss -tin (peer {ip}) ──")
            out_lines.extend(matched)
    except Exception as e:
        out_lines.append(f"ss failed: {e}")

    nfsd_clients = "/proc/fs/nfsd/clients"
    if os.path.isdir(nfsd_clients):
        try:
            for cid in sorted(os.listdir(nfsd_clients)):
                info_path = os.path.join(nfsd_clients, cid, "info")
                try:
                    with open(info_path) as f:
                        info = f.read()
                except Exception:
                    continue
                if ip in info:
                    out_lines.append("")
                    out_lines.append(f"── /proc/fs/nfsd/clients/{cid}/info ──")
                    out_lines.extend(info.rstrip().splitlines())
        except Exception:
            pass

    if not out_lines:
        out_lines.append(f"(no detail available for {ip})")

    return "\n".join(out_lines)


def draw_text_popup(stdscr, title: str, text: str) -> None:
    height, width = stdscr.getmaxyx()
    raw_lines = text.splitlines() or [""]

    longest = max((len(l) for l in raw_lines), default=0)
    box_w   = min(max(40, longest + 4, len(title) + 4), max(4, width - 2))
    box_h   = min(len(raw_lines) + 2, max(3, height - 2))
    if box_w < 4 or box_h < 3:
        return
    row0  = max(0, (height - box_h) // 2)
    col0  = max(0, (width  - box_w) // 2)
    inner = box_w - 2

    C_BOX  = attr_for("title")
    C_TEXT = attr_for("text")

    if inner >= len(title):
        pad   = inner - len(title)
        ldash = pad // 2
        rdash = pad - ldash
        top   = "┌" + "─" * ldash + title + "─" * rdash + "┐"
    else:
        top = "┌" + title[:inner] + "┐"
    safe_addstr(stdscr, row0, col0, top, C_BOX)

    bottom_row = row0 + box_h - 1
    for idx, line in enumerate(raw_lines):
        r = row0 + 1 + idx
        if r >= bottom_row or r >= height:
            break
        safe_addstr(stdscr, r, col0, "│" + " " * inner + "│", C_BOX)
        safe_addstr(stdscr, r, col0 + 1, line[:inner], C_TEXT)
        safe_addstr(stdscr, r, col0 + box_w - 1, "│", C_BOX)

    if bottom_row < height:
        safe_addstr(stdscr, bottom_row, col0, "└" + "─" * inner + "┘", C_BOX)


def aggregate_by_subnet(conns: List[Dict]) -> List[Dict]:
    # Bucket per IPv4 /24. The "host" label becomes "10.0.10.0/24"; numeric
    # fields sum up, age is the oldest first_seen in the group.
    groups: Dict[str, Dict] = {}
    for c in conns:
        parts = c["ip"].split(".")
        if len(parts) == 4:
            subnet = ".".join(parts[:3]) + ".0/24"
        else:
            subnet = c["ip"]
        g = groups.get(subnet)
        if g is None:
            g = {
                "ip": subnet, "host": subnet,
                "conns": 0, "age": c["age"],
                "sent": 0, "recv": 0, "dsent": 0, "drecv": 0,
                "rate_sent": 0.0, "rate_recv": 0.0, "activity": 0.0,
                "rtt_avg": 0.0,    # left at 0 → empty cell in subnet rows
            }
            groups[subnet] = g
        g["conns"] += c["conns"]
        g["sent"]  += c["sent"]
        g["recv"]  += c["recv"]
        g["dsent"] += c["dsent"]
        g["drecv"] += c["drecv"]
        g["rate_sent"] += c.get("rate_sent", 0.0)
        g["rate_recv"] += c.get("rate_recv", 0.0)
        g["activity"]  += c.get("activity",  0.0)
        if c["age"] < g["age"]:
            g["age"] = c["age"]
    return list(groups.values())


def write_snapshot(path: str, visible: List[Dict], total_count: int,
                   active_cols, sort_key: str, sort_rev: bool,
                   filter_str: str, group_subnet: bool,
                   followed_ip: Optional[str], paused: bool,
                   csv_enabled: bool) -> None:
    # Plain-text snapshot of the currently visible (filtered/grouped/sorted)
    # view, written to `path`. Mirrors the on-screen rendering using the
    # same column metadata so the dump matches what the user is looking at.
    arrow = "↓" if sort_rev else "↑"
    state_parts = [f"sort:{sort_key}{arrow}"]
    if filter_str:    state_parts.append(f"filter:{filter_str}")
    if group_subnet:  state_parts.append("group:/24")
    if followed_ip:   state_parts.append(f"follow:{followed_ip}")
    if paused:        state_parts.append("PAUSED")
    if csv_enabled:   state_parts.append("CSV")

    lines: List[str] = [
        f"NFS Traffic snapshot  port:{PORT}  "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "  ".join(state_parts),
        "",
    ]

    # column header + underline rule
    hdr_parts = [f"{label:{align}{w}}" for _, label, w, align, _ in active_cols]
    hdr = " ".join(hdr_parts)
    lines.append(hdr)
    lines.append("-" * len(hdr))

    # data rows
    for c in visible:
        parts = []
        for _, _, w, align, fn in active_cols:
            try:
                s = fn(c)
            except Exception:
                s = ""
            if len(s) > w:
                s = s[:w]
            parts.append(f"{s:{align}{w}}")
        lines.append(" ".join(parts))

    # footer totals
    ts = bytes_human(sum(c["sent"] for c in visible))
    tr = bytes_human(sum(c["recv"] for c in visible))
    tc = sum(c["conns"] for c in visible)
    lines.append("")
    if filter_str:
        lines.append(f"clients:{len(visible)}/{total_count}  "
                     f"conns:{tc}  sent:{ts}  recv:{tr}")
    else:
        lines.append(f"clients:{total_count}  total conns:{tc}  "
                     f"sent:{ts}  recv:{tr}")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def draw(stdscr, visible: List[Dict], total_count: int,
         sort_key: str, sort_rev: bool,
         start_time: float, show_help: bool,
         filter_str: str, filter_input_active: bool,
         group_subnet: bool, show_ip_in_host: bool,
         selected_ip: Optional[str],
         paused: bool, detail_text: str,
         cols_visible: Dict[str, bool],
         show_cols_popup: bool, cols_sel_idx: int,
         active_tab_idx: int,
         focus_zone: str,
         followed_ip: Optional[str],
         followed_snapshot: Optional[Dict],
         interval: int,
         edit_field: Optional[str],
         edit_input: str,
         csv_enabled: bool,
         csv_path: str,
         snapshot_msg: str,
         alert_active: Dict[str, bool],
         alert_mb: int,
         alert_dur: int,
         current_theme: str = "",
         bright_sort_col: bool = False,
         show_seen_clients: bool = False,
         watchdog_enabled: bool = False,
         watchdog_path: str = "",
         spark_len: int = SPARK_LEN) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()

    C_TITLE  = attr_for("title")
    C_FOOTER = attr_for("footer")

    # title row
    title = (f" NFS Traffic  port:{PORT}"
             f"  {time.strftime('%H:%M:%S')}"
             f"  interval:{interval}s"
             f"  since:{time.strftime('%H:%M:%S', time.localtime(start_time))}"
             f"  ({elapsed_str(start_time)}) ")
    safe_addstr(stdscr, 0, 0, title.ljust(width)[:width - 1], C_TITLE)

    # column header — built from visible columns; HOST entry is swapped for
    # IP when show_ip_in_host is True (handled in _build_active_cols).
    active_cols = _build_active_cols(cols_visible, show_ip_in_host, spark_len)
    hdr_parts = [f"{label:{align}{w}}" for _, label, w, align, _ in active_cols]
    hdr = " " + " ".join(hdr_parts)
    safe_addstr(stdscr, 1, 0, hdr.ljust(width)[:width - 1],
                curses.A_BOLD | curses.A_UNDERLINE)

    # ghost row: when following an IP that's not currently visible, we
    # render a stale snapshot at the very bottom data row. Reserve that row
    # by stopping regular rendering one line earlier.
    visible_ips  = {c["ip"] for c in visible}
    ghost_active = bool(followed_ip and followed_snapshot
                        and followed_ip not in visible_ips)
    last_row     = (height - 3) if ghost_active else (height - 2)

    # bright-sort-column overlay: identify the active_cols index whose id
    # matches sort_key. Special case: when show_ip_in_host is True, sorting
    # by "ip" highlights the host column (because that column shows the IP).
    # If no visible column matches (e.g. sort_key="activity"), no overlay.
    sort_col_idx = None
    if bright_sort_col:
        for i, (cid, _, _, _, _) in enumerate(active_cols):
            if cid == sort_key or (sort_key == "ip" and cid == "host"
                                    and show_ip_in_host):
                sort_col_idx = i
                break
    # Pre-compute the column's start column and width for the overlay.
    if sort_col_idx is not None:
        sort_col_w     = active_cols[sort_col_idx][2]
        sort_col_start = 1 + sum(active_cols[j][2] for j in range(sort_col_idx)) + sort_col_idx
    else:
        sort_col_w = sort_col_start = 0

    # data rows — color each row by current throughput rate;
    # selected row gets reverse-video on top of the activity color.
    for idx, c in enumerate(visible):
        row = 2 + idx
        if row > last_row:
            break
        parts = []
        for _, _, w, align, fn in active_cols:
            s = fn(c)
            if len(s) > w:
                s = s[:w]
            parts.append(f"{s:{align}{w}}")
        line = " " + " ".join(parts)
        rate = c.get("rate_sent", 0.0) + c.get("rate_recv", 0.0)
        if alert_active.get(c["ip"]):
            attr = attr_for("alert")
        else:
            attr = activity_attr(rate)
        if c["ip"] == selected_ip:
            attr |= curses.A_REVERSE
        safe_addstr(stdscr, row, 0, line[:width - 1], attr)
        if sort_col_idx is not None and sort_col_start < width - 1:
            # Strip A_DIM and OR in A_BOLD so the active sort column
            # renders as the bright variant on most terminals.
            bright_attr = (attr & ~curses.A_DIM) | curses.A_BOLD
            seg_end     = min(sort_col_start + sort_col_w, width - 1)
            safe_addstr(stdscr, row, sort_col_start,
                        line[sort_col_start:seg_end], bright_attr)

    # ghost row for the followed-but-vanished IP
    if ghost_active:
        parts = []
        for _, _, w, align, fn in active_cols:
            try:
                s = fn(followed_snapshot)
            except Exception:
                s = ""
            if len(s) > w:
                s = s[:w]
            parts.append(f"{s:{align}{w}}")
        line = " " + " ".join(parts)
        attr = attr_for("idle") | curses.A_REVERSE
        safe_addstr(stdscr, height - 2, 0, line[:width - 1], attr)
        if sort_col_idx is not None and sort_col_start < width - 1:
            bright_attr = (attr & ~curses.A_DIM) | curses.A_BOLD
            seg_end     = min(sort_col_start + sort_col_w, width - 1)
            safe_addstr(stdscr, height - 2, sort_col_start,
                        line[sort_col_start:seg_end], bright_attr)

    # footer: totals (filtered if filter active) + right-side hint or input prompt
    ts = bytes_human(sum(c["sent"] for c in visible))
    tr = bytes_human(sum(c["recv"] for c in visible))
    tc = sum(c["conns"] for c in visible)
    if filter_str or filter_input_active:
        totals = (f" clients:{len(visible)}/{total_count}"
                  f"  conns:{tc}  sent:{ts}  recv:{tr}")
    else:
        totals = f" clients:{total_count}  total conns:{tc}  sent:{ts}  recv:{tr}"
    arrow = "↓" if sort_rev else "↑"
    if filter_input_active:
        hint = f"/{filter_str}_  Enter:apply  Esc:cancel "
    elif snapshot_msg:
        # Overrides the normal hint for SNAPSHOT_TIMEOUT seconds after `D`.
        hint = snapshot_msg + " "
    else:
        f_part   = f"  filter:{filter_str}" if filter_str else ""
        g_part   = "  group:/24" if group_subnet else ""
        p_part   = "  PAUSED" if paused else ""
        fol_part = f"  follow:{followed_ip}" if followed_ip else ""
        csv_part = "  CSV" if csv_enabled else ""
        alert_n  = sum(1 for v in alert_active.values() if v)
        a_part   = f"  ALERT:{alert_n}" if alert_n > 0 else ""
        hint     = (f"sort:{sort_key}{arrow}{f_part}{g_part}{fol_part}{p_part}{csv_part}{a_part}"
                    f"  /:filter  f:follow  g:group  o:opts  space:pause  h:help  q:quit ")
    gap    = max(1, width - len(totals) - len(hint))
    footer = (totals + " " * gap + hint).ljust(width)[:width - 1]
    safe_addstr(stdscr, height - 1, 0, footer, C_FOOTER)

    # Draw popup(s) BEFORE the single refresh so background and popup
    # appear in one frame — avoids flicker between two refresh calls.
    if show_help:
        draw_help_popup(stdscr)
    if show_cols_popup:
        draw_columns_popup(stdscr, cols_visible, sort_key, cols_sel_idx,
                           active_tab_idx, focus_zone,
                           interval, edit_field, edit_input,
                           csv_enabled, csv_path,
                           alert_mb, alert_dur,
                           current_theme,
                           bright_sort_col,
                           show_seen_clients,
                           watchdog_enabled,
                           watchdog_path,
                           spark_len)
    if detail_text:
        draw_text_popup(stdscr, " Connection Detail ", detail_text)

    stdscr.refresh()


def _resolve_color(name: str) -> int:
    # Accepts: a base/256-color name from _color_name_map(), OR a numeric
    # color code "0".."255". Numbers win over names — "208" returns 208,
    # not whatever happens to be in the name map. Unknown values → -1
    # (terminal default fg). Out-of-range numbers also clamp to -1; the
    # >= curses.COLORS guard in setup_colors() additionally falls back if
    # the terminal can't render this code.
    s = (name or "").strip().lower()
    if not s:
        return -1
    try:
        n = int(s)
        if 0 <= n <= 255:
            return n
    except ValueError:
        pass
    return _color_name_map().get(s, -1)


def _resolve_attrs(spec: str) -> int:
    out = 0
    amap = _attr_name_map()
    for tok in (spec or "").replace(" ", "").split(","):
        if not tok:
            continue
        out |= amap.get(tok.lower(), 0)
    return out


def setup_colors(overrides: Optional[Dict[str, Tuple[str, str]]] = None) -> None:
    # Build effective role table from defaults + overrides. Each role gets a
    # unique curses pair number; resulting attr (pair | flags) is cached in
    # _ROLE_ATTR so call sites can do attr_for("title") without caring about
    # pair numbers. Re-callable: theme switch at runtime calls this again with
    # a fresh overrides dict.
    #
    # 256-color graceful fallback: if a theme requests a color the terminal
    # can't render (fg >= curses.COLORS, e.g. on an 8-color tty), we drop
    # that override and use the role's built-in default. If the default is
    # also out of range we use -1 (terminal default fg).
    curses.start_color()
    curses.use_default_colors()
    max_colors = max(getattr(curses, "COLORS", 8), 8)
    effective  = dict(DEFAULT_COLORS)
    if overrides:
        for role, val in overrides.items():
            if role in effective and isinstance(val, tuple) and len(val) == 2:
                effective[role] = val
    _ROLE_ATTR.clear()
    pair = 1
    for role, (fg_name, attr_spec) in effective.items():
        fg = _resolve_color(fg_name)
        if fg >= max_colors:
            default_fg = _resolve_color(DEFAULT_COLORS[role][0])
            fg = default_fg if default_fg < max_colors else -1
        try:
            curses.init_pair(pair, fg, -1)
        except curses.error:
            # Some terminals reject high pair numbers; fall back to no pair.
            _ROLE_ATTR[role] = _resolve_attrs(attr_spec)
            continue
        _ROLE_ATTR[role] = curses.color_pair(pair) | _resolve_attrs(attr_spec)
        pair += 1


def attr_for(role: str) -> int:
    return _ROLE_ATTR.get(role, 0)


def activity_attr(rate_bps: float) -> int:
    # idle / low / medium / high color tiers, used for per-row coloring.
    if rate_bps <= 0:
        return attr_for("idle")
    if rate_bps < 1024 * 1024:                     # < 1 MB/s
        return attr_for("activity_low")
    if rate_bps < 10 * 1024 * 1024:                # < 10 MB/s
        return attr_for("activity_medium")
    return attr_for("activity_high")


def main(stdscr) -> None:
    global PORT                       # mutable so the [defaults] tab can edit it
    curses.curs_set(0)
    stdscr.nodelay(True)

    # Load persisted settings: CONFIG_SYSTEM (system) overridden by
    # CONFIG_USER (user). Missing keys fall back to the built-in defaults
    # below. Colors are loaded before setup_colors so any theme / [colors]
    # overrides apply on the very first paint.
    cfg = load_config()
    if "port" in cfg:
        PORT = cfg["port"]

    current_theme   = cfg.get("theme", "") or ""
    # base_color_overrides keeps the stable [colors] from the main config.
    # color_overrides = base + active theme; recomputed on every theme switch.
    base_color_overrides: Dict[str, Tuple[str, str]] = dict(cfg.get("colors", {}))
    color_overrides:      Dict[str, Tuple[str, str]] = dict(base_color_overrides)
    color_overrides.update(load_theme(current_theme))
    setup_colors(color_overrides)

    sort_key            = cfg.get("sort_key", "sent")
    sort_rev            = cfg.get("sort_rev", True)
    bright_sort_col     = cfg.get("bright_sort_col", False)  # bright-overlay on active sort col
    spark_len           = cfg.get("spark_len", SPARK_LEN)    # TREND column width 10-40
    if spark_len < SPARK_MIN_LEN or spark_len > SPARK_MAX_LEN:
        spark_len = SPARK_LEN                                 # clamp out-of-range from config
    show_help           = False
    conns               = []
    last_tick           = 0.0
    start_time          = time.time()
    first_seen          = {}   # ip -> timestamp of first appearance
    last_seen: Dict[str, float] = {}   # ip -> timestamp of latest appearance (live + ghost)
    seen_snapshot: Dict[str, Dict] = {}   # ip -> last-known conn dict (rates zeroed)
    show_seen_clients   = cfg.get("show_seen_clients", False)
    baseline            = {}   # ip -> {"sent": X, "recv": X} at first sight
    prev_totals         = {}   # ip -> {"sent": X, "recv": X, "ts": float} for rate calc
    rate_history: Dict[str, List[float]] = {}   # ip -> rolling list of last SPARK_LEN total rates
    filter_str          = ""   # active filter (substring matched on host + ip)
    filter_input_active = False
    filter_saved        = ""   # snapshot to restore on Esc
    group_subnet        = cfg.get("group_subnet", False)
    show_ip_in_host     = cfg.get("show_ip", False)   # Shift+i toggles HOST col → IP
    paused              = False
    frozen_conns        = None # snapshot taken at pause; None = follow live
    selected_ip         = None # currently highlighted row (by IP)
    detail_text         = ""   # non-empty → detail popup is open
    last_input_time     = time.time()  # for 5s auto-hide of selection
    running             = True
    PAGE_STEP           = 5    # rows per PgUp/PgDn jump
    SELECTION_TIMEOUT   = 5.0  # seconds without input → hide selection

    # column visibility — defaults: all on except B/s, TREND, NFSv, MOUNT, RTT
    # (off by default). Persisted overrides from cfg apply on top.
    cols_visible: Dict[str, bool] = {
        col_id: col_id not in ("rate_sent", "rate_recv", "spark", "nfsv",
                               "mount", "rtt_avg", "last_seen")
        for col_id, *_ in COLUMNS
    }
    cols_visible.update(cfg.get("cols_visible", {}))
    show_cols_popup = False
    active_tab_idx  = 0       # 0..len(TABS)-1 — always a real tab
    focus_zone      = "main"  # "main" (tab bar + body items) or "save"
    cols_sel_idx    = 0

    # tick interval, port, and alert thresholds are editable via the Options
    # popup; only one is in edit-submode at a time, tracked by edit_field /
    # edit_input. edit_field ∈ {None, "interval", "port", "alert_mb", "alert_dur"}.
    interval = cfg.get("interval", INTERVAL)
    edit_field: Optional[str] = None
    edit_input: str           = ""

    # CSV logging — toggled via [defaults] tab. If config says enabled, try
    # opening at startup; on failure keep disabled (silent).
    csv_enabled = cfg.get("csv_enabled", False)
    csv_path    = cfg.get("csv_path", CSV_DEFAULT_PATH)
    csv_file    = None
    csv_writer  = None
    if csv_enabled:
        try:
            csv_file, csv_writer = _open_csv(csv_path)
        except OSError:
            csv_enabled = False

    # Watchdog — logs disconnect events (IPs that were live last tick but
    # not this tick) to a separate CSV-shaped log. Same open-on-startup +
    # silent-fail-on-OSError semantics as csv logging.
    watchdog_enabled = cfg.get("watchdog_enabled", False)
    watchdog_path    = cfg.get("watchdog_path", WATCHDOG_DEFAULT_PATH)
    watchdog_file    = None
    watchdog_writer  = None
    if watchdog_enabled:
        try:
            watchdog_file, watchdog_writer = _open_watchdog(watchdog_path)
        except OSError:
            watchdog_enabled = False

    # Flash-message — non-empty for SNAPSHOT_TIMEOUT seconds after a flash
    # event (snapshot dump, config save, ...), auto-cleared in the main loop.
    snapshot_msg       = ""
    snapshot_msg_until = 0.0

    # Alert thresholds — editable via [defaults] tab. If activity (= live
    # rate_sent + rate_recv) exceeds alert_mb*1024*1024 B/s for >= alert_dur
    # seconds, alert_active[ip] = True. Resets when activity drops below.
    alert_mb  = cfg.get("alert_mb",  ALERT_DEFAULT_MB)
    alert_dur = cfg.get("alert_dur", ALERT_DEFAULT_DUR)
    alert_since:  Dict[str, float] = {}
    alert_active: Dict[str, bool]  = {}

    # follow mode: when set, the selection is pinned to that IP (no auto-hide,
    # no drop on disappearance). If the IP leaves the visible set, a ghost
    # row is rendered at the bottom from the last known snapshot.
    followed_ip:       Optional[str]  = None
    followed_snapshot: Optional[Dict] = None

    while running:
        now = time.time()

        # auto-clear the snapshot flash-message after its timeout
        if snapshot_msg and now >= snapshot_msg_until:
            snapshot_msg = ""

        # ----- tick: collect data continuously; pause only freezes display -
        if now - last_tick >= interval:
            raw        = get_connections()
            active_ips = {c["ip"] for c in raw}

            for c in raw:
                if c["ip"] not in first_seen:
                    first_seen[c["ip"]]        = now
                    baseline[c["ip"]]          = {"sent": c["sent"], "recv": c["recv"]}

            # Watchdog: emit a disconnect event for every IP that was live
            # last tick but not this tick. first_seen at this point still
            # contains last tick's live set (plus the newly-added IPs from
            # the loop just above, which are subset of active_ips and so
            # cancel out under set-difference). Snapshot data for the host /
            # totals comes from seen_snapshot, populated during the previous
            # tick. Silent on OSError so a full disk doesn't crash the TUI.
            if watchdog_enabled and watchdog_writer is not None:
                disconnected = set(first_seen.keys()) - active_ips
                if disconnected:
                    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
                    try:
                        for ip in disconnected:
                            snap = seen_snapshot.get(ip, {})
                            watchdog_writer.writerow([
                                ts_iso, "disconnect", ip,
                                snap.get("host", ip),
                                snap.get("sent", 0),
                                snap.get("recv", 0),
                            ])
                        watchdog_file.flush()
                    except OSError:
                        pass

            # remove stale IPs from all per-ip state
            first_seen   = {ip: t for ip, t in first_seen.items()   if ip in active_ips}
            baseline     = {ip: v for ip, v in baseline.items()     if ip in active_ips}
            prev_totals  = {ip: v for ip, v in prev_totals.items()  if ip in active_ips}
            rate_history = {ip: h for ip, h in rate_history.items() if ip in active_ips}
            alert_since  = {ip: t for ip, t in alert_since.items()  if ip in active_ips}
            alert_active = {ip: v for ip, v in alert_active.items() if ip in active_ips}

            # compute current B/s rate per IP from this tick vs the previous one.
            new_prev = {}
            rates    = {}
            for c in raw:
                ip = c["ip"]
                p  = prev_totals.get(ip)
                if p:
                    dt = max(now - p["ts"], 1e-6)
                    rates[ip] = (
                        max(0.0, (c["sent"] - p["sent"]) / dt),
                        max(0.0, (c["recv"] - p["recv"]) / dt),
                    )
                else:
                    rates[ip] = (0.0, 0.0)
                new_prev[ip] = {"sent": c["sent"], "recv": c["recv"], "ts": now}
            prev_totals = new_prev

            # update sparkline history (one combined-rate sample per IP per tick).
            # Buffer length tracks the runtime spark_len so widening the column
            # in the popup makes more history visible on the next tick.
            for c in raw:
                ip = c["ip"]
                h  = rate_history.setdefault(ip, [])
                h.append(rates[ip][0] + rates[ip][1])
                if len(h) > spark_len:
                    del h[: len(h) - spark_len]

            # NFS-server-side per-client info (NFSv version, ...) — keyed by IP
            nfsd_info = read_nfsd_clients_info()

            # merge age + delta + rate + spark + nfsv + mount + activity
            # into each connection. `activity` = combined live rate, used as
            # a dedicated sort key (with sort_rev=False → idle first).
            conns = [
                {
                    **c,
                    "age":       first_seen.get(c["ip"], now),
                    "dsent":     c["sent"] - baseline.get(c["ip"], {}).get("sent", c["sent"]),
                    "drecv":     c["recv"] - baseline.get(c["ip"], {}).get("recv", c["recv"]),
                    "rate_sent": rates[c["ip"]][0],
                    "rate_recv": rates[c["ip"]][1],
                    "activity":  rates[c["ip"]][0] + rates[c["ip"]][1],
                    "spark":     sparkline(rate_history.get(c["ip"], []), spark_len),
                    "nfsv":      nfsd_info.get(c["ip"], {}).get("nfsv", ""),
                    "mount":     nfsd_info.get(c["ip"], {}).get("mount", ""),
                    "last_seen": now,
                }
                for c in raw
            ]
            # last_seen + seen_snapshot persist across disconnect: when an IP
            # falls off `raw`, we still know the last time we saw it and a
            # frozen view of its last live state (rates/conns/activity zeroed).
            # The "Show last-seen clients" toggle in the [defaults] tab uses
            # these to render ghost rows below the live ones.
            for c in conns:
                ip = c["ip"]
                last_seen[ip] = now
                snap = dict(c)
                snap["rate_sent"] = 0.0
                snap["rate_recv"] = 0.0
                snap["activity"]  = 0.0
                snap["conns"]     = 0
                snap["spark"]     = " " * spark_len
                seen_snapshot[ip] = snap
            last_tick = now

            # CSV logging — one row per peer per tick when enabled. Wrapped
            # in try/except so a full disk or vanished file doesn't crash
            # the TUI; the toggle stays on but writes silently fail.
            if csv_enabled and csv_writer is not None:
                ts_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    for c in conns:
                        csv_writer.writerow([
                            ts_iso, c["ip"], c["host"],
                            c["sent"], c["recv"],
                            c["dsent"], c["drecv"],
                            f"{c['rate_sent']:.2f}",
                            f"{c['rate_recv']:.2f}",
                            c["conns"],
                        ])
                    csv_file.flush()
                except OSError:
                    pass

            # alert detection — activity above threshold for >= duration
            # flips alert_active[ip] to True; reset when activity drops.
            alert_threshold_bps = alert_mb * 1024 * 1024
            for c in conns:
                ip = c["ip"]
                if c["activity"] >= alert_threshold_bps:
                    if ip not in alert_since:
                        alert_since[ip] = now
                    if now - alert_since[ip] >= alert_dur:
                        alert_active[ip] = True
                else:
                    alert_since.pop(ip, None)
                    alert_active.pop(ip, None)

            # refresh ghost snapshot for the followed IP if it's still here
            if followed_ip:
                for c in conns:
                    if c["ip"] == followed_ip:
                        followed_snapshot = c
                        break

        # ----- compute current view -----------------------------------------
        display_source = frozen_conns if (paused and frozen_conns is not None) else conns
        visible        = compute_visible(display_source, filter_str, group_subnet,
                                         sort_key, sort_rev)
        # Append ghost rows for previously-seen clients that aren't currently
        # live. Always below the live set, regardless of sort. Skips
        # group_subnet aggregation so the ghost rows stay individually
        # identifiable. Filter (if any) still applies.
        if show_seen_clients:
            live_ips    = {c["ip"] for c in display_source}
            ghost_list  = [seen_snapshot[ip] for ip in last_seen
                           if ip not in live_ips and ip in seen_snapshot]
            if filter_str:
                f = filter_str.lower()
                ghost_list = [c for c in ghost_list
                              if f in c["host"].lower() or f in c["ip"]]
            ghost_list = sorted(ghost_list,
                                key=lambda x: x.get(sort_key, 0) if isinstance(x.get(sort_key), (int, float))
                                              else x.get(sort_key, ""),
                                reverse=sort_rev)
            visible = visible + ghost_list

        # drop selection if no longer present in visible set (unless followed —
        # then we keep selected_ip pointing at the followed IP and ghost-render)
        if (selected_ip and not followed_ip
                and not any(c["ip"] == selected_ip for c in visible)):
            selected_ip = None

        # ----- drain ALL queued keys this iteration -------------------------
        # nodelay() + a 100ms sleep means a held arrow key buffers ~3 events
        # per tick. Processing one per iteration causes "scroll-on" after
        # release. Instead we drain everything pending and process it all in
        # this frame — the buffer is empty by the time we sleep again.
        keys = []
        while True:
            k = stdscr.getch()
            if k == -1:
                break
            keys.append(k)

        if keys:
            last_input_time = now

        # ----- auto-hide selection after idle period (suspended in follow) --
        if (selected_ip and not followed_ip
                and (now - last_input_time) > SELECTION_TIMEOUT):
            selected_ip = None

        # ----- process every key --------------------------------------------
        for key in keys:
            if key == curses.KEY_RESIZE:
                stdscr.clear()
            elif filter_input_active:
                if key == 27:                                          # Esc
                    filter_str          = filter_saved
                    filter_input_active = False
                elif key in (10, 13, curses.KEY_ENTER):                # Enter
                    filter_input_active = False
                elif key in (8, 127, curses.KEY_BACKSPACE):
                    filter_str = filter_str[:-1]
                elif 32 <= key < 127:
                    filter_str += chr(key)
            elif detail_text:                                          # detail popup is open
                if key in (27, ord("q"), 10, 13, curses.KEY_ENTER):
                    detail_text = ""
            elif show_cols_popup:                                      # options popup is open
                # focus_zone toggles between "main" (tab bar + body items)
                # and "save" (the [Save Settings] button at bottom-right).
                # In main: ←/→ switches active_tab_idx, ↑/↓ navigates items,
                # Space activates the focused item, Tab → save.
                # In save: Space saves (stays), Enter saves+closes, Tab → main.
                save_focused = focus_zone == "save"
                tab_items    = TABS[active_tab_idx][2]

                if edit_field is not None:                             # numeric input submode
                    if edit_field == "port":
                        max_len = 5
                    elif edit_field == "spark_len":
                        max_len = 2     # max value 40 fits in 2 digits
                    else:
                        max_len = 4
                    if key in (10, 13, curses.KEY_ENTER):              # apply
                        try:
                            v = int(edit_input)
                            if edit_field == "interval" and 1 <= v <= 9999:
                                interval = v
                            elif edit_field == "port" and 1 <= v <= 65535:
                                PORT = v
                            elif edit_field == "alert_mb" and 1 <= v <= 9999:
                                alert_mb = v
                            elif edit_field == "alert_dur" and 1 <= v <= 9999:
                                alert_dur = v
                            elif edit_field == "spark_len" and SPARK_MIN_LEN <= v <= SPARK_MAX_LEN:
                                spark_len = v
                        except ValueError:
                            pass
                        edit_field = None
                        edit_input = ""
                    elif key == 27:                                    # cancel
                        edit_field = None
                        edit_input = ""
                    elif key in (8, 127, curses.KEY_BACKSPACE):
                        edit_input = edit_input[:-1]
                    elif (ord("0") <= key <= ord("9")
                          and len(edit_input) < max_len):
                        edit_input += chr(key)
                elif key in (27, ord("q"), ord("o")):                  # always close
                    show_cols_popup = False
                elif key in (10, 13, curses.KEY_ENTER):
                    if save_focused:                                   # Enter on save = save+close
                        if save_config(interval, PORT, group_subnet,
                                       show_ip_in_host,
                                       sort_key, sort_rev,
                                       csv_enabled, csv_path,
                                       cols_visible, alert_mb, alert_dur,
                                       theme=current_theme,
                                       colors=color_overrides,
                                       bright_sort_col=bright_sort_col,
                                       show_seen_clients=show_seen_clients,
                                       watchdog_enabled=watchdog_enabled,
                                       watchdog_path=watchdog_path,
                                       spark_len=spark_len):
                            snapshot_msg = f"config saved → {CONFIG_USER}"
                        else:
                            snapshot_msg = "config save failed"
                        snapshot_msg_until = now + SNAPSHOT_TIMEOUT
                    show_cols_popup = False
                elif key in (9, curses.KEY_BTAB):                      # Tab/⇧Tab toggles main↔save
                    focus_zone = "save" if focus_zone == "main" else "main"
                elif key == curses.KEY_LEFT:                           # ←: prev tab (in main)
                    if not save_focused:
                        active_tab_idx = (active_tab_idx - 1) % len(TABS)
                        cols_sel_idx   = 0
                elif key == curses.KEY_RIGHT:                          # →: next tab (in main)
                    if not save_focused:
                        active_tab_idx = (active_tab_idx + 1) % len(TABS)
                        cols_sel_idx   = 0
                elif key == curses.KEY_UP:
                    if not save_focused and cols_sel_idx > 0:
                        cols_sel_idx -= 1
                elif key == curses.KEY_DOWN:
                    if not save_focused and cols_sel_idx < len(tab_items) - 1:
                        cols_sel_idx += 1
                elif key == ord(" "):
                    if save_focused:                                   # Space on save = save (stay)
                        if save_config(interval, PORT, group_subnet,
                                       show_ip_in_host,
                                       sort_key, sort_rev,
                                       csv_enabled, csv_path,
                                       cols_visible, alert_mb, alert_dur,
                                       theme=current_theme,
                                       colors=color_overrides,
                                       bright_sort_col=bright_sort_col,
                                       show_seen_clients=show_seen_clients,
                                       watchdog_enabled=watchdog_enabled,
                                       watchdog_path=watchdog_path,
                                       spark_len=spark_len):
                            snapshot_msg = f"config saved → {CONFIG_USER}"
                        else:
                            snapshot_msg = "config save failed"
                        snapshot_msg_until = now + SNAPSHOT_TIMEOUT
                    else:
                        item = tab_items[cols_sel_idx]
                        if item[0] == "show":
                            cols_visible[item[1]] = not cols_visible.get(item[1], True)
                        elif item[0] == "sort":
                            sort_key = item[1]
                        elif item[0] == "interval":
                            edit_field = "interval"
                            edit_input = str(interval)
                        elif item[0] == "port":
                            edit_field = "port"
                            edit_input = str(PORT)
                        elif item[0] == "csv":
                            if csv_enabled:
                                try:
                                    if csv_file:
                                        csv_file.close()
                                except Exception:
                                    pass
                                csv_file    = None
                                csv_writer  = None
                                csv_enabled = False
                            else:
                                try:
                                    csv_file, csv_writer = _open_csv(csv_path)
                                    csv_enabled = True
                                except OSError:
                                    pass   # silent fail (e.g. permission denied)
                        elif item[0] == "alert_mb":
                            edit_field = "alert_mb"
                            edit_input = str(alert_mb)
                        elif item[0] == "alert_dur":
                            edit_field = "alert_dur"
                            edit_input = str(alert_dur)
                        elif item[0] == "theme":
                            # Cycle through "" (defaults) + every theme file
                            # found in THEME_DIR. Re-runs setup_colors so the
                            # change is visible on the next paint without
                            # restarting the program.
                            theme_list = [""] + list_themes()
                            try:
                                cur_idx = theme_list.index(current_theme)
                            except ValueError:
                                cur_idx = 0
                            current_theme   = theme_list[(cur_idx + 1) % len(theme_list)]
                            color_overrides = dict(base_color_overrides)
                            color_overrides.update(load_theme(current_theme))
                            setup_colors(color_overrides)
                        elif item[0] == "bright":
                            bright_sort_col = not bright_sort_col
                        elif item[0] == "seen":
                            show_seen_clients = not show_seen_clients
                        elif item[0] == "watchdog":
                            if watchdog_enabled:
                                try:
                                    if watchdog_file:
                                        watchdog_file.close()
                                except Exception:
                                    pass
                                watchdog_file    = None
                                watchdog_writer  = None
                                watchdog_enabled = False
                            else:
                                try:
                                    watchdog_file, watchdog_writer = _open_watchdog(watchdog_path)
                                    watchdog_enabled = True
                                except OSError:
                                    pass   # silent fail (e.g. permission denied)
                        elif item[0] == "spark_len":
                            edit_field = "spark_len"
                            edit_input = str(spark_len)
                elif key == ord("v"):                                  # toggle sort dir from popup
                    sort_rev = not sort_rev
            elif key == 27 and show_help:                              # Esc closes help
                show_help = False
            elif key == ord("q"):
                running = False
                break
            elif key == ord("h"):
                show_help = not show_help
            elif key == ord("/"):
                filter_saved        = filter_str
                filter_input_active = True
                show_help           = False
            elif key == ord("g"):
                group_subnet = not group_subnet
            elif key == ord("I"):
                # Toggle the host column between hostname (DNS) and raw IP;
                # column header swaps "HOST" ↔ "IP" accordingly. The standalone
                # IP column was removed in favour of this toggle.
                show_ip_in_host = not show_ip_in_host
                show_help       = False
            elif key == ord("o"):
                show_cols_popup = True
                show_help       = False
                active_tab_idx  = 0
                focus_zone      = "main"
                cols_sel_idx    = 0
            elif key == ord("f"):
                # Toggle follow mode. Pin selection to an IP so it persists
                # across re-sorts, filter changes, and disappearance (ghost).
                if followed_ip:
                    followed_ip       = None
                    followed_snapshot = None
                    selected_ip       = None
                elif selected_ip and any(c["ip"] == selected_ip for c in visible):
                    followed_ip = selected_ip
                    for c in visible:
                        if c["ip"] == selected_ip:
                            followed_snapshot = c
                            break
                elif visible:
                    selected_ip       = visible[0]["ip"]
                    followed_ip       = selected_ip
                    followed_snapshot = visible[0]
            elif key == ord(" "):
                paused = not paused
                frozen_conns = list(conns) if paused else None
            elif key in (curses.KEY_UP, curses.KEY_DOWN,
                         curses.KEY_PPAGE, curses.KEY_NPAGE) and visible:
                ips = [c["ip"] for c in visible]
                if key == curses.KEY_UP:
                    step = -1
                elif key == curses.KEY_DOWN:
                    step = 1
                elif key == curses.KEY_PPAGE:
                    step = -PAGE_STEP
                else:
                    step = PAGE_STEP
                if selected_ip in ips:
                    i = ips.index(selected_ip) + step
                else:
                    i = 0 if step > 0 else len(ips) - 1
                i = max(0, min(len(ips) - 1, i))
                selected_ip = ips[i]
                if followed_ip:                        # follow target tracks selection
                    followed_ip       = selected_ip
                    followed_snapshot = visible[i]
            elif key in (10, 13, curses.KEY_ENTER) and visible:
                target = selected_ip if selected_ip in [c["ip"] for c in visible] else visible[0]["ip"]
                detail_text = fetch_detail(target)
                selected_ip = target
                break                                                  # popup opened; stop draining
            elif key == ord("A"):
                sort_key  = "activity"
                sort_rev  = False                  # idle first
                show_help = False
            elif 0 <= key < 256 and chr(key) in KEY_MAP:
                sort_key  = KEY_MAP[chr(key)]
                sort_rev  = True
                show_help = False
            elif key == ord("v"):
                sort_rev = not sort_rev
            elif key == ord("D"):
                # Snapshot dump: write the current visible view as plain
                # text. Footer flashes a confirmation/error for
                # SNAPSHOT_TIMEOUT seconds.
                active_cols = _build_active_cols(cols_visible, show_ip_in_host, spark_len)
                try:
                    write_snapshot(SNAPSHOT_PATH, visible, len(display_source),
                                   active_cols, sort_key, sort_rev,
                                   filter_str, group_subnet,
                                   followed_ip, paused, csv_enabled)
                    snapshot_msg       = f"snapshot saved → {SNAPSHOT_PATH}"
                except OSError as e:
                    snapshot_msg       = f"snapshot failed: {e}"
                snapshot_msg_until = now + SNAPSHOT_TIMEOUT
            elif key == ord("z"):
                # Reset: zero out deltas, connection age, the "since" timer
                # and clear sparkline history.
                start_time = now
                first_seen.clear()
                baseline.clear()
                rate_history.clear()
                for c in conns:
                    first_seen[c["ip"]] = now
                    baseline[c["ip"]]   = {"sent": c["sent"], "recv": c["recv"]}
                    c["age"]   = now
                    c["dsent"] = 0
                    c["drecv"] = 0
                    c["spark"] = " " * spark_len

        if not running:
            break

        # ----- render -------------------------------------------------------
        draw(stdscr, visible, len(display_source), sort_key, sort_rev, start_time,
             show_help, filter_str, filter_input_active,
             group_subnet, show_ip_in_host,
             selected_ip, paused, detail_text,
             cols_visible, show_cols_popup, cols_sel_idx,
             active_tab_idx, focus_zone,
             followed_ip, followed_snapshot,
             interval, edit_field, edit_input,
             csv_enabled, csv_path,
             snapshot_msg,
             alert_active, alert_mb, alert_dur,
             current_theme,
             bright_sort_col,
             show_seen_clients,
             watchdog_enabled,
             watchdog_path,
             spark_len)
        time.sleep(0.1)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="nfsmon",
        description="NFSv4 server-side traffic monitor (curses TUI).",
        add_help=True,
    )
    p.add_argument(
        "--version", "-V",
        action="version",
        version=f"nfsmon v{__version__} {__author__}",
    )
    return p.parse_args()


if __name__ == "__main__":
    _parse_args()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
