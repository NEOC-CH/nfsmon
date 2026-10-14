#!/usr/bin/env python3
# nfsmon.py

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

SNAPSHOT_DIR = "/tmp"    # d writes nfsmon_snapshot_<YYYYmmdd-HHMMSS>.txt here
SNAPSHOT_TIMEOUT = 3.0   # seconds the footer flash-message stays visible

CONFIG_SYSTEM = "/etc/nfsmon.conf"                            # system-wide defaults
CONFIG_USER   = os.path.expanduser("~/.config/nfsmon.conf")   # per-user override + save target
THEME_DIR     = os.path.expanduser("~/.config/nfsmon/colors") # per-user theme files: <name>.conf

ALERT_DEFAULT_MB  = 10    # default alert threshold in MB/s
ALERT_DEFAULT_DUR = 5     # default alert duration in seconds (above threshold)

WATCHDOG_DEFAULT_PATH = "/var/log/nfsmon-events.log"
WATCHDOG_HEADER = ["timestamp", "event", "ip", "host", "last_sent", "last_recv"]
WATCHDOG_RECONNECT_SEC = 300   # a client back within this window logs "reconnect", else "connect"

MOUSE_STEP       = 3           # rows per mouse-wheel notch
MOUSE_WHEEL_UP   = getattr(curses, "BUTTON4_PRESSED", 0x10000)
MOUSE_WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)   # BUTTON5_* needs Python >= 3.10

CLI_OVERRIDES: Dict[str, int] = {}   # --port / --interval from argparse, applied over the config

ESC_DELAY_MS = 25              # ncurses waits this long after a lone Esc for a sequence (default 1000!)

DETAIL_TITLE = " Connection Detail "
DETAIL_HSTEP = 8               # columns per ←/→ step in the detail popup

NFSD_THREADS_PATH = "/proc/fs/nfsd/threads"
RPC_NFSD_PATH     = "/proc/net/rpc/nfsd"

DNS_TTL_OK   = 3600   # seconds a successful reverse lookup is cached
DNS_TTL_FAIL = 300    # seconds before a failed reverse lookup is retried

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
                      ("spark_len", "int"),
                      ("mouse", "bool"),
                      ("server_stats", "bool"),
                      ("seen_max_hours", "int")):
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
                spark_len: int = SPARK_LEN,
                mouse: bool = True,
                server_stats: bool = True,
                seen_max_hours: int = 0) -> bool:
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
        "mouse":             "true" if mouse else "false",
        "server_stats":      "true" if server_stats else "false",
        "seen_max_hours":    str(seen_max_hours),
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


# --- data-source health -----------------------------------------------------
# Data sources degrade silently (empty rows/columns) rather than crash, but
# the user still deserves to know *why* a column is empty. Each source sets
# or clears its own entry; draw() shows the texts in the title row.
_health: Dict[str, str] = {}


def _set_health(source: str, msg: str) -> None:
    if msg:
        _health[source] = msg
    else:
        _health.pop(source, None)


def health_warnings() -> List[str]:
    return [_health[k] for k in sorted(_health)]


# --- async DNS resolution ---------------------------------------------------
# gethostbyaddr can stall multiple seconds on first lookup; doing it on the
# UI thread froze input. Resolve in a daemon worker; callers get the IP
# back immediately and the resolved name appears on a later redraw.
# Cache entries are (name, expires_at). A failed lookup is cached as the
# bare IP for DNS_TTL_FAIL seconds and then retried, so a DNS hiccup at
# startup no longer pins IPs into the HOST column until restart. Successes
# expire after DNS_TTL_OK so renamed hosts eventually show up too; the old
# name stays on screen while the refresh is pending.
_dns_cache:   Dict[str, Tuple[str, float]] = {}
_dns_pending: Set[str]                     = set()
_dns_lock                                  = threading.Lock()
_dns_queue:   "Queue[str]"                 = Queue()


def _dns_worker() -> None:
    while True:
        ip = _dns_queue.get()
        try:
            name, ttl = socket.gethostbyaddr(ip)[0], DNS_TTL_OK
        except Exception:
            name, ttl = ip, DNS_TTL_FAIL
        with _dns_lock:
            _dns_cache[ip] = (name, time.time() + ttl)
            _dns_pending.discard(ip)


threading.Thread(target=_dns_worker, daemon=True).start()


def resolve_host(ip: str) -> str:
    now = time.time()
    with _dns_lock:
        entry = _dns_cache.get(ip)
        if entry is not None and (now < entry[1] or ip in _dns_pending):
            return entry[0]
        if ip in _dns_pending:
            return ip
        _dns_pending.add(ip)
    _dns_queue.put(ip)
    return entry[0] if entry is not None else ip


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
    except FileNotFoundError:
        _set_health("ss", "ss not found (install iproute2)")
        return []
    except subprocess.TimeoutExpired:
        _set_health("ss", "ss timed out")
        return []
    except Exception:
        _set_health("ss", "ss failed")
        return []
    _set_health("ss", "")

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


NFSD_CLIENTS_DIR   = "/proc/fs/nfsd/clients"
EXPORT_TABLE_PATHS = ("/proc/fs/nfsd/exports", "/proc/fs/nfs/exports", "/etc/exports")


def _unescape_export_path(s: str) -> str:
    # nfsd writes blanks and specials in export paths as \ooo (octal).
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _export_devices() -> Dict[Tuple[int, int], List[str]]:
    # (major, minor) -> exported paths living on that filesystem. Read from
    # the kernel's export table, falling back to /etc/exports; a path that
    # cannot be stat'ed is skipped. A handful of lines + stats per tick.
    out: Dict[Tuple[int, int], List[str]] = {}
    for table in EXPORT_TABLE_PATHS:
        try:
            with open(table) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = _unescape_export_path(line.split()[0])
            if not p.startswith("/"):
                continue
            try:
                st = os.stat(p)
            except OSError:
                continue
            dev = (os.major(st.st_dev), os.minor(st.st_dev))
            if p not in out.setdefault(dev, []):
                out[dev].append(p)
        if out:
            break
    return out


def _mount_from_exports(devs: Set[Tuple[int, int]],
                        export_devs: Dict[Tuple[int, int], List[str]]) -> str:
    # Exports whose filesystem matches the device of an open file. Drop
    # candidates that are ancestors of other candidates (the fsid=0
    # pseudo-root pattern), then reduce what is left to one string.
    cands: List[str] = []
    for dev in devs:
        for p in export_devs.get(dev, []):
            if p not in cands:
                cands.append(p)
    cands = [p for p in cands
             if not any(q != p and q.startswith(p.rstrip("/") + "/") for q in cands)]
    if not cands:
        return ""
    if len(cands) == 1:
        return cands[0]
    cands.sort()
    prefix = _common_dir_prefix(cands)
    return prefix if prefix not in ("", "/") else ",".join(cands)


def read_nfsd_clients_info(need_states: bool = True) -> Dict[str, Dict]:
    # Parse /proc/fs/nfsd/clients/<id>/{info,states} per peer. Cheap small
    # reads (~30 files even on a busy server). Returns ip -> {"nfsv": "4.2",
    # "mount": "/export/home", "states": (o, l, d), "client":
    # "<name the client reported>", "kernel": "<client kernel release>"}.
    # mount is derived from the client's open files in `states`:
    #   * kernels that print absolute filenames -> longest common directory
    #     prefix of those paths;
    #   * kernels that print only the last two path components (RHEL 9's
    #     5.14, mainline 6.x) -> the superblock "maj:min" of the open files
    #     mapped onto the export table (see _mount_from_exports).
    # Empty if the client has no open state or nothing can be mapped.
    # "states" is (opens, locks, delegations) counted from the same file,
    # or None when it was not read. need_states=False skips the
    # (comparatively large) states files when neither MOUNT nor OPEN is
    # visible or the sort key.
    result: Dict[str, Dict[str, str]] = {}
    base = NFSD_CLIENTS_DIR
    if not os.path.isdir(base):
        _set_health("nfsd", f"no {base} (nfsd not running?)")
        return result
    try:
        cids = os.listdir(base)
    except OSError:
        _set_health("nfsd", f"no access to {base} (run as root)")
        return result
    export_devs: Optional[Dict[Tuple[int, int], List[str]]] = None   # loaded on first need
    denied = 0
    for cid in cids:
        info_path   = os.path.join(base, cid, "info")
        states_path = os.path.join(base, cid, "states")
        try:
            with open(info_path) as f:
                info = f.read()
        except PermissionError:
            denied += 1
            continue
        except OSError:
            continue
        ip      = None
        version = None
        client  = ""
        kernel  = ""
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("name:"):
                # client id string; Linux clients send "Linux NFSv4.x <hostname>"
                m = re.search(r'"([^"]*)"', line)
                if m:
                    client = m.group(1)
                    lm = re.match(r"Linux NFSv4\.\d+ (.+)$", client)
                    if lm:
                        client = lm.group(1)
            elif line.startswith("Implementation name:"):
                # e.g. "Linux 6.8.0-142-generic #142-Ubuntu SMP ..." → kernel release
                m = re.search(r'"([^"]*)"', line)
                if m:
                    toks = m.group(1).split()
                    kernel = toks[1] if len(toks) >= 2 and toks[0] == "Linux" else m.group(1)
            elif line.startswith("address:"):
                m = re.search(r'"([^"]+)"', line)
                if m:
                    # strip trailing :port (works for both IPv4 "1.2.3.4:0"
                    # and bracketed IPv6 "[::1]:0")
                    ip = re.sub(r":\d+$", "", m.group(1))
            elif line.startswith("minor version:"):
                v = line.split(":", 1)[1].strip()
                if v.isdigit():
                    version = f"4.{v}"
        mount  = ""
        states = ""
        detail: Optional[Tuple[int, int, int]] = None
        if need_states:
            try:
                with open(states_path) as f:
                    states = f.read()
            except OSError:
                pass
        if states:
            kinds  = re.findall(r"type: (\w+)", states)
            detail = (kinds.count("open"), kinds.count("lock"), kinds.count("deleg"))
            # A bare "/" (an open directory handle on the fs root) carries no
            # information and would collapse the common prefix, so skip it.
            paths = [p for p in re.findall(r'"(/[^"]+)"', states) if p != "/"]
            if paths:
                mount = _common_dir_prefix(paths)
            else:
                devs = {(int(a, 16), int(b, 16)) for a, b in
                        re.findall(r'superblock: "([0-9a-fA-F]+):([0-9a-fA-F]+):\d+"', states)}
                if devs:
                    if export_devs is None:
                        export_devs = _export_devices()
                    mount = _mount_from_exports(devs, export_devs)
        if ip:
            result[ip] = {"nfsv": version or "", "mount": mount, "states": detail,
                          "client": client, "kernel": kernel}
    if denied and not result:
        _set_health("nfsd", f"no access to {base}/*/info (run as root)")
    else:
        _set_health("nfsd", "")
    return result


def read_server_stats() -> Dict[str, int]:
    # Server-wide nfsd counters (raw, monotonic). main() turns them into
    # per-tick rates via server_rates_from(). Missing files → fewer keys.
    out: Dict[str, int] = {}
    try:
        with open(NFSD_THREADS_PATH) as f:
            out["threads"] = int(f.read().strip())
    except (OSError, ValueError):
        pass
    try:
        with open(RPC_NFSD_PATH) as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and p[0] == "io":          # NFS payload bytes read / written
                    out["io_read"], out["io_write"] = int(p[1]), int(p[2])
                elif len(p) >= 3 and p[0] == "rpc":       # calls, bad calls (fmt+auth+clnt), ...
                    out["rpc"], out["bad_rpc"] = int(p[1]), int(p[2])
                elif len(p) >= 2 and p[0] == "fh":        # stale file handles, ...
                    out["stale_fh"] = int(p[1])
                elif len(p) >= 2 and p[0] == "th" and "threads" not in out:
                    out["threads"] = int(p[1])
    except (OSError, ValueError):
        pass
    return out


def server_rates_from(prev: Dict, cur: Dict, now: float) -> Dict:
    # Per-second rates between two read_server_stats() samples (prev carries
    # its timestamp as "ts"). Counters that wrapped or are missing yield no
    # rate rather than nonsense.
    out: Dict = {"threads": cur.get("threads")}
    if not prev or "ts" not in prev:
        return out
    dt = max(now - prev["ts"], 1e-6)
    for key, name in (("rpc", "rpc_s"), ("io_read", "read_s"), ("io_write", "write_s"),
                      ("bad_rpc", "badrpc_s"), ("stale_fh", "stale_s")):
        if key in cur and key in prev and cur[key] >= prev[key]:
            out[name] = (cur[key] - prev[key]) / dt
    return out


def fmt_rate(x: float) -> str:
    # compact count rate: 198 → "198", 1234 → "1.2k", 2.5e6 → "2.5M"
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.1f}k"
    return f"{x:.0f}"


def server_stats_text(r: Dict) -> str:
    # One-line server summary for the title row / snapshot header. Error
    # counters only appear while they actually tick.
    if not r:
        return ""
    parts = []
    if r.get("threads") is not None:
        parts.append(f"nfsd:{r['threads']}thr")
    if r.get("rpc_s") is not None:
        parts.append(f"rpc:{fmt_rate(r['rpc_s'])}/s")
    if r.get("read_s") is not None:
        parts.append(f"rd:{bytes_human(int(r['read_s']))}/s")
    if r.get("write_s") is not None:
        parts.append(f"wr:{bytes_human(int(r['write_s']))}/s")
    if r.get("badrpc_s"):
        parts.append(f"badrpc:{fmt_rate(r['badrpc_s'])}/s")
    if r.get("stale_s"):
        parts.append(f"stalefh:{fmt_rate(r['stale_s'])}/s")
    return "  ".join(parts)


def server_stats_warn(r: Dict) -> bool:
    return bool(r.get("badrpc_s") or r.get("stale_s"))


def prune_seen(last_seen: Dict[str, float], seen_snapshot: Dict[str, Dict], cutoff: float) -> int:
    # Opt-in cap for the last-seen memory: forget clients not seen since
    # `cutoff`. Live clients refresh last_seen every tick and never qualify.
    stale = [ip for ip, ts in last_seen.items() if ts < cutoff]
    for ip in stale:
        last_seen.pop(ip, None)
        seen_snapshot.pop(ip, None)
    return len(stale)


# ---------- curses UI -------------------------------------------------------

# Every sort hotkey is Shift+<letter>; lowercase letters are actions.
# ACTIVITY (idle first) is handled separately in main() because it sorts
# ascending by default.
KEY_MAP     = {"S": "sent",      "X": "dsent",     "B": "rate_sent",
               "R": "recv",      "Y": "drecv",     "W": "rate_recv",
               "C": "conns",     "A": "age",
               "I": "ip",        "H": "host",      "P": "client",
               "N": "nfsv",      "K": "kernel",    "M": "mount",
               "O": "opens",     "L": "locks",     "D": "delegs",
               "T": "rtt_avg",   "Z": "last_seen"}


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


def count_str(n) -> str:
    # Per-client counters (OPEN/LCK/DLG): blank instead of "0" so the rows
    # that actually hold something stand out; None (not read) is blank too.
    return str(n) if n else ""


def host_cell(c: Dict) -> str:
    # Reverse-DNS name; while that is still the bare IP fall back to the
    # name the client put into its NFSv4 client id, marked with "~".
    host = c["host"]
    if host == c["ip"] and c.get("client"):
        return "~" + c["client"]
    return host


# Column definitions drive the table header and data rows. Each entry is
# (id, label, width, align, value_fn). cols_visible[id] gates whether the
# column is rendered; the order in COLUMNS is the on-screen order.
COLUMNS = [
    ("host",      "HOST",      30, "<", host_cell),
    ("client",    "CLIENT",    24, "<", lambda c: c.get("client", "")),
    ("nfsv",      "NFSv",       4, "<", lambda c: c.get("nfsv", "")),
    ("kernel",    "KERNEL",    18, "<", lambda c: c.get("kernel", "")),
    ("mount",     "MOUNT",     24, "<", lambda c: c.get("mount", "")),
    ("opens",     "OPEN",       5, ">", lambda c: count_str(c.get("opens"))),
    ("locks",     "LCK",        4, ">", lambda c: count_str(c.get("locks"))),
    ("delegs",    "DLG",        4, ">", lambda c: count_str(c.get("delegs"))),
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


TITLE_ROW      = 0   # screen layout: title, blank spacer, column header, data...
HEADER_ROW     = 2
FIRST_DATA_ROW = 3
SCROLL_TRACK   = "│"  # scrollbar track glyph (last screen column)
SCROLL_THUMB   = "█"  # scrollbar thumb glyph


def data_rows(height: int, ghost_active: bool) -> int:
    # Number of data rows that fit between the column header and the footer.
    # The follow-ghost row (when shown) reserves the line above the footer.
    # Single source of truth for draw() and the viewport logic in main().
    reserved = FIRST_DATA_ROW + 1 + (1 if ghost_active else 0)   # rows above data + footer (+ ghost)
    return max(1, height - reserved)


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
        ("watchdog",  "watchdog_enabled", "Watchdog (log events)"),
        ("mouse",     "mouse",            "Mouse (wheel scrolls, click selects)"),
        ("sstats",    "server_stats",     "Server stats in title row"),
        ("seen_max",  "seen_max_hours",   "Last-seen max age (h, 0=keep)"),
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
    ("─── Sort (all Shift+letter) ──────────────", None),
    ("  Shift+s",   "SENT (total)"),
    ("  Shift+x",   "ΔSENT (since baseline)"),
    ("  Shift+b",   "B/s SENT (live rate)"),
    ("  Shift+r",   "RECV (total)"),
    ("  Shift+y",   "ΔRECV (since baseline)"),
    ("  Shift+w",   "B/s RECV (live rate, client writes)"),
    ("  Shift+c",   "CONNS"),
    ("  Shift+a",   "CONNECTED (age)"),
    ("  Shift+v",   "ACTIVITY (idle first)"),
    ("  Shift+i",   "IP"),
    ("  Shift+h",   "HOST (DNS name)"),
    ("  Shift+p",   "CLIENT (peer-reported name)"),
    ("  Shift+n",   "NFSv"),
    ("  Shift+k",   "KERNEL (client kernel)"),
    ("  Shift+m",   "MOUNT"),
    ("  Shift+o",   "OPEN (open files)"),
    ("  Shift+l",   "LCK (locks)"),
    ("  Shift+d",   "DLG (delegations)"),
    ("  Shift+t",   "RTT (latency)"),
    ("  Shift+z",   "SEEN (last seen)"),
    ("  v",         "Toggle sort direction ↑↓"),
    ("─── View ─────────────────────────────────", None),
    ("  /",         "Filter by host or IP (substring)"),
    ("  ↑/↓",       "Select row (view scrolls along)"),
    ("  PgUp/PgDn", "Select ±10 rows"),
    ("  Home/End",  "Jump to first / last row (vim: gg / G)"),
    ("  ←/→",       "Scroll columns (first column stays)"),
    ("  Mouse",     "Wheel scrolls 3 rows, click selects"),
    ("  Enter",     "Show detail (↑↓←→ scroll, Esc closes)"),
    ("  f",         "Follow selection (pin / unpin)"),
    ("  g",         "Group by /24 subnet (gg jumps to top)"),
    ("  i",         "Toggle host column: hostname ↔ IP"),
    ("─── Actions ──────────────────────────────", None),
    ("  o",         "Open Options popup"),
    ("  d",         f"Snapshot dump → {SNAPSHOT_DIR}/nfsmon_snapshot_<time>.txt"),
    ("  space",     "Pause / resume display"),
    ("  z",         "Reset baseline (zero Δ + age + since)"),
    ("─── General ──────────────────────────────", None),
    ("  h",         "Toggle this help"),
    ("  q",         "Quit"),
    ("  Esc",       "Close popup / cancel input"),
    ("──────────────────────────────────────────", None),
    ("TREND has no sort key; use the [sort] tab in Options.", ""),
    ("ΔSENT/ΔRECV = bytes since baseline (z resets).",      ""),
]


def safe_addstr(stdscr, row: int, col: int, text: str, attr: int = 0) -> None:
    # Writing to the bottom-right cell or past terminal width raises
    # curses.error. Swallow it — partial draws are fine, crashes aren't.
    try:
        stdscr.addstr(row, col, text, attr)
    except curses.error:
        pass


def popup_box_dims(height: int, width: int) -> Tuple[int, int]:
    # Fixed footprint shared by the help and Options popups (70×40, clamped
    # to the terminal) so switching between them doesn't make the screen
    # jump. main() uses it to clamp the help scroll offset.
    return min(40, max(8, height - 2)), min(70, max(20, width - 4))


def help_max_scroll(height: int, width: int) -> int:
    # HELP_LINES beyond the box scroll with ↑/↓, PgUp/PgDn, Home/End, wheel.
    box_h, _ = popup_box_dims(height, width)
    return max(0, len(HELP_LINES) - (box_h - 2))


def draw_help_popup(stdscr, scroll: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    box_h, box_w = popup_box_dims(height, width)
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

    # content rows (HELP_LINES) — overlay on the painted background. When
    # the list overflows, the last inner column carries a scrollbar (same
    # look as the main table) so the overflow is visible at a glance.
    last_content = row0 + box_h - 2   # last writable row above bottom border
    max_scroll   = help_max_scroll(height, width)
    scroll       = max(0, min(scroll, max_scroll))
    page_rows    = box_h - 2
    bar          = max_scroll > 0
    for idx, (left, right) in enumerate(HELP_LINES[scroll:]):
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
            desc_max = (col0 + box_w - 1) - desc_col - (1 if bar else 0)
            if desc_max > 0:
                safe_addstr(stdscr, r, desc_col, right[:desc_max], C_TEXT)
    if bar:
        total    = len(HELP_LINES)
        thumb_h  = max(1, round(page_rows * page_rows / total))
        thumb_at = round(scroll * (page_rows - thumb_h) / max_scroll)
        bar_col  = col0 + box_w - 2
        for i in range(page_rows):
            r = row0 + 1 + i
            if r > last_content or r >= height:
                break
            if thumb_at <= i < thumb_at + thumb_h:
                safe_addstr(stdscr, r, bar_col, SCROLL_THUMB, attr_for("footer") | curses.A_BOLD)
            else:
                safe_addstr(stdscr, r, bar_col, SCROLL_TRACK, attr_for("idle"))

    # bottom border — carries the scroll position when the list overflows
    bottom_row = row0 + box_h - 1
    if bottom_row < height:
        if help_max_scroll(height, width) > 0:
            shown  = min(len(HELP_LINES), scroll + box_h - 2)
            tag    = f" ↑↓ PgUp/PgDn scroll  {scroll + 1}-{shown}/{len(HELP_LINES)} "[:inner]
            pad    = inner - len(tag)
            bottom = "└" + "─" * (pad // 2) + tag + "─" * (pad - pad // 2) + "┘"
        else:
            bottom = "└" + "─" * inner + "┘"
        safe_addstr(stdscr, bottom_row, col0, bottom, C_BOX)


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
                       spark_len: int = SPARK_LEN,
                       mouse_enabled: bool = True,
                       server_stats_enabled: bool = True,
                       seen_max_hours: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    # Fixed target size (70×40, W×H) clamped to terminal so the popup never
    # changes size between tabs. Width was bumped from 60 to 70 to fit the
    # [Save Settings] button at the bottom-right next to the hint text.
    box_h, box_w = popup_box_dims(height, width)
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
        elif kind == "mouse":
            label = item[2]
            mark  = "[x]" if mouse_enabled else "[ ]"
            text  = f" {label}:  {mark}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "sstats":
            label = item[2]
            mark  = "[x]" if server_stats_enabled else "[ ]"
            text  = f" {label}:  {mark}".ljust(inner)[:inner]
            attr  = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
            safe_addstr(stdscr, r, col0 + 1, text, attr)
        elif kind == "seen_max":
            label = item[2]
            if edit_field == "seen_max" and idx == sel_idx:
                val_str = (edit_input + "_")[:5]
            else:
                val_str = str(seen_max_hours)
            text = f" {label}:  [{val_str:>4}]".ljust(inner)[:inner]
            attr = C_TEXT | (curses.A_REVERSE if idx == sel_idx else 0)
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
                       if f in c["host"].lower() or f in c["ip"]
                          or f in c.get("client", "").lower()]
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


def text_popup_geometry(text: str, title: str, height: int, width: int) -> Dict:
    # Box placement and scroll limits for the detail popup, shared by the
    # renderer and by main()'s key handling. Content wider/taller than the
    # box scrolls: vertical bar in the last inner column, horizontal via
    # ←/→ (DETAIL_HSTEP columns per step).
    lines   = text.splitlines() or [""]
    longest = max((len(l) for l in lines), default=0)
    box_w   = min(max(40, longest + 4, len(title) + 4), max(4, width - 2))
    box_h   = min(len(lines) + 2, max(3, height - 2))
    page    = box_h - 2
    vbar    = len(lines) > page
    text_w  = max(1, (box_w - 2) - (1 if vbar else 0))
    return {
        "lines": lines, "longest": longest, "box_w": box_w, "box_h": box_h,
        "row0": max(0, (height - box_h) // 2), "col0": max(0, (width - box_w) // 2),
        "page": page, "vbar": vbar, "text_w": text_w,
        "max_y": max(0, len(lines) - page), "max_x": max(0, longest - text_w),
    }


def draw_text_popup(stdscr, title: str, text: str, scroll_y: int = 0, scroll_x: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    g = text_popup_geometry(text, title, height, width)
    box_w, box_h, row0, col0 = g["box_w"], g["box_h"], g["row0"], g["col0"]
    if box_w < 4 or box_h < 3:
        return
    inner    = box_w - 2
    scroll_y = max(0, min(scroll_y, g["max_y"]))
    scroll_x = max(0, min(scroll_x, g["max_x"]))

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
    page, text_w = g["page"], g["text_w"]
    for idx, line in enumerate(g["lines"][scroll_y:scroll_y + page]):
        r = row0 + 1 + idx
        if r >= bottom_row or r >= height:
            break
        safe_addstr(stdscr, r, col0, "│" + " " * inner + "│", C_BOX)
        safe_addstr(stdscr, r, col0 + 1, line[scroll_x:scroll_x + text_w], C_TEXT)
        safe_addstr(stdscr, r, col0 + box_w - 1, "│", C_BOX)
    if g["vbar"]:
        total    = len(g["lines"])
        thumb_h  = max(1, round(page * page / total))
        thumb_at = round(scroll_y * (page - thumb_h) / g["max_y"]) if g["max_y"] else 0
        for i in range(page):
            r = row0 + 1 + i
            if r >= bottom_row or r >= height:
                break
            glyph, attr = ((SCROLL_THUMB, attr_for("footer") | curses.A_BOLD)
                           if thumb_at <= i < thumb_at + thumb_h
                           else (SCROLL_TRACK, attr_for("idle")))
            safe_addstr(stdscr, r, col0 + box_w - 2, glyph, attr)

    if bottom_row < height:
        tags = []
        if g["vbar"]:
            tags.append(f"↑↓ rows {scroll_y + 1}-{min(len(g['lines']), scroll_y + page)}/{len(g['lines'])}")
        if g["max_x"] > 0:
            tags.append(f"←→ cols {scroll_x + 1}-{min(g['longest'], scroll_x + text_w)}/{g['longest']}")
        if tags:
            tag    = (" " + "  ".join(tags) + " ")[:inner]
            pad    = inner - len(tag)
            bottom = "└" + "─" * (pad // 2) + tag + "─" * (pad - pad // 2) + "┘"
        else:
            bottom = "└" + "─" * inner + "┘"
        safe_addstr(stdscr, bottom_row, col0, bottom, C_BOX)


def aggregate_by_subnet(conns: List[Dict]) -> List[Dict]:
    # Bucket per IPv4 /24. The "host" label becomes "192.0.2.0/24"; numeric
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
                # Non-aggregatable fields stay present-but-empty (blank
                # cells) so every sort_key remains usable while grouped
                # instead of raising KeyError in compute_visible.
                "nfsv": "", "mount": "", "spark": "", "last_seen": 0.0,
                "client": "", "kernel": "",
                "opens": 0, "locks": 0, "delegs": 0,
            }
            groups[subnet] = g
        g["conns"] += c["conns"]
        g["opens"]  += c.get("opens", 0)
        g["locks"]  += c.get("locks", 0)
        g["delegs"] += c.get("delegs", 0)
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


def snapshot_path(ts: Optional[float] = None) -> str:
    # One file per dump so consecutive d presses don't overwrite.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))
    return os.path.join(SNAPSHOT_DIR, f"nfsmon_snapshot_{stamp}.txt")


def _apply_mouse(enabled: bool) -> None:
    # Mouse reporting on/off at runtime; terminals without mouse support
    # just never deliver KEY_MOUSE. Shift+drag keeps native text selection.
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS if enabled else 0)
    except curses.error:
        pass


def _live_rows(visible: List[Dict]) -> List[Dict]:
    # Footer and snapshot totals count live clients only. Last-seen ghost
    # rows (flagged "ghost" when stored in seen_snapshot) are display-only;
    # summing them would add bytes of clients that left long ago.
    return [c for c in visible if not c.get("ghost")]


def write_snapshot(path: str, visible: List[Dict], total_count: int,
                   active_cols, sort_key: str, sort_rev: bool,
                   filter_str: str, group_subnet: bool,
                   followed_ip: Optional[str], paused: bool,
                   csv_enabled: bool, server_line: str = "") -> None:
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
    state_parts.extend(f"!{w}" for w in health_warnings())

    lines: List[str] = [
        f"NFS Traffic snapshot  port:{PORT}  "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "  ".join(state_parts),
        "",
    ]
    if server_line:
        lines.insert(2, server_line)

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

    # footer totals (live rows only, see _live_rows)
    live = _live_rows(visible)
    ts = bytes_human(sum(c["sent"] for c in live))
    tr = bytes_human(sum(c["recv"] for c in live))
    tc = sum(c["conns"] for c in live)
    lines.append("")
    if filter_str:
        lines.append(f"clients:{len(live)}/{total_count}  "
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
         spark_len: int = SPARK_LEN,
         scroll_top: int = 0,
         col_offset: int = 0,
         help_scroll: int = 0,
         mouse_enabled: bool = True,
         server_line: str = "",
         server_warn: bool = False,
         server_stats_enabled: bool = True,
         seen_max_hours: int = 0,
         detail_y: int = 0,
         detail_x: int = 0) -> None:
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
    safe_addstr(stdscr, TITLE_ROW, 0, title.ljust(width)[:width - 1], C_TITLE)
    used  = len(title)
    warns = health_warnings()
    if warns and used + 2 < width - 1:
        # data-source problems (no ss, no /proc/fs/nfsd access) — shown in
        # the warning tier so an empty column is never a silent mystery
        seg = (" ! " + "  ! ".join(warns) + " ")[:width - 1 - used - 1]
        safe_addstr(stdscr, TITLE_ROW, used + 1, seg, attr_for("activity_medium"))
        used += 1 + len(seg)
    if server_line:
        # server-wide nfsd stats, right-aligned in the title row; when the
        # row is too narrow they move to the spacer row below it
        attr  = attr_for("activity_medium") if server_warn else C_FOOTER
        s_row = TITLE_ROW if used + 2 + len(server_line) <= width - 1 else TITLE_ROW + 1
        seg   = server_line[-(width - 1):]
        safe_addstr(stdscr, s_row, max(0, width - 1 - len(seg)), seg, attr)

    # column header — built from visible columns; HOST entry is swapped for
    # IP when show_ip_in_host is True (handled in _build_active_cols).
    active_cols = _build_active_cols(cols_visible, show_ip_in_host, spark_len)
    # Horizontal column scrolling (←/→): the first column stays pinned,
    # col_offset hides that many of the following columns on the left.
    # "‹" after the pinned column / "›" at the right edge mark hidden ones.
    shown_cols = active_cols[:1] + active_cols[1 + col_offset:]
    hdr_raw    = " " + " ".join(f"{label:{align}{w}}" for _, label, w, align, _ in shown_cols)
    cut_right  = len(hdr_raw) > width - 1
    hdr        = hdr_raw.ljust(width)[:width - 1]
    if col_offset > 0 and shown_cols:
        p = 1 + shown_cols[0][2]          # separator right after the pinned column
        if p < len(hdr):
            hdr = hdr[:p] + "‹" + hdr[p + 1:]
    if cut_right and len(hdr) >= 2:
        hdr = hdr[:-1] + "›"
    safe_addstr(stdscr, HEADER_ROW, 0, hdr, curses.A_BOLD | curses.A_UNDERLINE)

    # ghost row: when following an IP that's not currently visible, we
    # render a stale snapshot at the very bottom data row. Reserve that row
    # by giving the regular rows one line less.
    visible_ips  = {c["ip"] for c in visible}
    ghost_active = bool(followed_ip and followed_snapshot
                        and followed_ip not in visible_ips)
    page_rows    = data_rows(height, ghost_active)

    # bright-sort-column overlay: identify the active_cols index whose id
    # matches sort_key. Special case: when show_ip_in_host is True, sorting
    # by "ip" highlights the host column (because that column shows the IP).
    # If no visible column matches (e.g. sort_key="activity"), no overlay.
    sort_col_idx = None
    if bright_sort_col:
        for i, (cid, _, _, _, _) in enumerate(shown_cols):
            if cid == sort_key or (sort_key == "ip" and cid == "host"
                                    and show_ip_in_host):
                sort_col_idx = i
                break
    # Pre-compute the column's start column and width for the overlay.
    if sort_col_idx is not None:
        sort_col_w     = shown_cols[sort_col_idx][2]
        sort_col_start = 1 + sum(shown_cols[j][2] for j in range(sort_col_idx)) + sort_col_idx
    else:
        sort_col_w = sort_col_start = 0

    # data rows — the viewport starts at scroll_top (main() keeps the
    # selected row inside it); color each row by current throughput rate,
    # selected row gets reverse-video on top of the activity color.
    for idx, c in enumerate(visible[scroll_top:scroll_top + page_rows]):
        row = FIRST_DATA_ROW + idx
        parts = []
        for _, _, w, align, fn in shown_cols:
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

    # scrollbar: data rows stop at width-2, so the last column is free. When
    # the list overflows the page, draw a track over the data rows with a
    # thumb whose size/position mirror the visible slice.
    total = len(visible)
    if total > page_rows and width >= 2:
        bar_col  = width - 1
        thumb_h  = max(1, round(page_rows * page_rows / total))
        max_top  = total - page_rows
        thumb_at = round(scroll_top * (page_rows - thumb_h) / max_top) if max_top else 0
        for i in range(page_rows):
            r = FIRST_DATA_ROW + i
            if r >= height - 1:
                break
            if thumb_at <= i < thumb_at + thumb_h:
                safe_addstr(stdscr, r, bar_col, SCROLL_THUMB, attr_for("footer") | curses.A_BOLD)
            else:
                safe_addstr(stdscr, r, bar_col, SCROLL_TRACK, attr_for("idle"))

    # ghost row for the followed-but-vanished IP
    if ghost_active:
        parts = []
        for _, _, w, align, fn in shown_cols:
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

    # footer: totals over the live rows (filtered if a filter is active;
    # ghost rows never count) + right-side hint or input prompt
    live = _live_rows(visible)
    ts = bytes_human(sum(c["sent"] for c in live))
    tr = bytes_human(sum(c["recv"] for c in live))
    tc = sum(c["conns"] for c in live)
    if filter_str or filter_input_active:
        totals = (f" clients:{len(live)}/{total_count}"
                  f"  conns:{tc}  sent:{ts}  recv:{tr}")
    else:
        totals = f" clients:{total_count}  total conns:{tc}  sent:{ts}  recv:{tr}"
    if len(visible) > page_rows:
        # list overflows the screen: show which slice is on display
        lo = scroll_top + 1
        hi = min(len(visible), scroll_top + page_rows)
        totals += f"  rows:{lo}-{hi}/{len(visible)}"
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
        c_part   = "  ←→:cols" if (cut_right or col_offset > 0) else ""
        hint     = (f"sort:{sort_key}{arrow}{f_part}{g_part}{fol_part}{p_part}{csv_part}{a_part}{c_part}"
                    f"  /:filter  f:follow  g:group  o:opts  space:pause  h:help  q:quit ")
    gap    = max(1, width - len(totals) - len(hint))
    footer = (totals + " " * gap + hint).ljust(width)[:width - 1]
    safe_addstr(stdscr, height - 1, 0, footer, C_FOOTER)

    # Draw popup(s) BEFORE the single refresh so background and popup
    # appear in one frame — avoids flicker between two refresh calls.
    if show_help:
        draw_help_popup(stdscr, help_scroll)
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
                           spark_len,
                           mouse_enabled,
                           server_stats_enabled,
                           seen_max_hours)
    if detail_text:
        draw_text_popup(stdscr, DETAIL_TITLE, detail_text, detail_y, detail_x)

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
    # A lone Esc must close popups at once. ncurses otherwise blocks the
    # whole loop for ESCDELAY (1 s by default) waiting for an escape
    # sequence. set_escdelay() needs Python >= 3.9; the environment variable
    # set in __main__ covers older interpreters.
    if hasattr(curses, "set_escdelay"):
        try:
            curses.set_escdelay(ESC_DELAY_MS)
        except curses.error:
            pass

    # Load persisted settings: CONFIG_SYSTEM (system) overridden by
    # CONFIG_USER (user). Missing keys fall back to the built-in defaults
    # below. Colors are loaded before setup_colors so any theme / [colors]
    # overrides apply on the very first paint.
    cfg = load_config()
    cfg.update(CLI_OVERRIDES)         # --port / --interval win over the config files
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
    mouse_enabled       = cfg.get("mouse", True)
    _apply_mouse(mouse_enabled)
    server_stats_enabled = cfg.get("server_stats", True)
    server_prev:  Dict  = {}   # last read_server_stats() sample + "ts"
    server_rates: Dict  = {}   # per-second rates for the title row
    seen_max_hours      = cfg.get("seen_max_hours", 0)       # 0 = keep last-seen forever
    if not 0 <= seen_max_hours <= 9999:
        seen_max_hours = 0
    baseline            = {}   # ip -> {"sent": X, "recv": X} at first sight
    prev_totals         = {}   # ip -> {"sent": X, "recv": X, "ts": float} for rate calc
    rate_history: Dict[str, List[float]] = {}   # ip -> rolling list of last SPARK_LEN total rates
    filter_str          = ""   # active filter (substring matched on host + ip)
    filter_input_active = False
    filter_saved        = ""   # snapshot to restore on Esc
    group_subnet        = cfg.get("group_subnet", False)
    show_ip_in_host     = cfg.get("show_ip", False)   # i toggles HOST col → IP
    paused              = False
    frozen_conns        = None # snapshot taken at pause; None = follow live
    selected_ip         = None # currently highlighted row (by IP)
    detail_text         = ""   # non-empty → detail popup is open
    detail_y            = 0    # detail popup scroll offsets (rows / columns)
    detail_x            = 0
    last_input_time     = time.time()  # for 5s auto-hide of selection
    running             = True
    PAGE_STEP           = 10   # rows per PgUp/PgDn jump
    SELECTION_TIMEOUT   = 5.0  # seconds without input → hide selection
    GG_TIMEOUT          = 0.5  # seconds a lone `g` waits for a second `g`
    scroll_top          = 0    # index of the first data row on screen
    col_offset          = 0    # columns hidden left of the table (←/→), first column pinned
    help_scroll         = 0    # first HELP_LINES entry shown in the help popup
    pending_g           = 0.0  # timestamp of a `g` awaiting `gg`; 0 = none

    # column visibility — defaults: all on except B/s, TREND, NFSv, MOUNT, RTT
    # (off by default). Persisted overrides from cfg apply on top.
    cols_visible: Dict[str, bool] = {
        col_id: col_id not in ("rate_sent", "rate_recv", "spark", "nfsv",
                               "mount", "rtt_avg", "last_seen",
                               "opens", "locks", "delegs", "client", "kernel")
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
    visible: List[Dict]               = []

    def _viewport_rows() -> int:
        # Data rows currently available on screen (mirrors draw()'s layout).
        height, _ = stdscr.getmaxyx()
        ghost = bool(followed_ip and followed_snapshot
                     and followed_ip not in {c["ip"] for c in visible})
        return data_rows(height, ghost)

    def _move_cursor(step: int) -> None:
        # Relative cursor move (arrows, PgUp/PgDn, wheel). Without a cursor
        # it appears at the edge of the current viewport: top for downward
        # moves, bottom for upward ones.
        if not visible:
            return
        ips = [c["ip"] for c in visible]
        if selected_ip in ips:
            i = ips.index(selected_ip) + step
        else:
            page = _viewport_rows()
            i = scroll_top if step > 0 else min(len(ips), scroll_top + page) - 1
        _select_index(i)

    def _mouse_event():
        # (bstate, x, y) of the pending KEY_MOUSE, or None when curses has
        # nothing for us (can happen with stale events after a mask change).
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return None
        return bstate, mx, my

    def _wd_event(event: str, ip: str, host: str, sent: int, recv: int) -> None:
        # One watchdog row (timestamp, event, ip, host, totals). Silent on
        # OSError so a full disk can't crash the TUI.
        if watchdog_writer is None:
            return
        try:
            watchdog_writer.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      event, ip, host, sent, recv])
            watchdog_file.flush()
        except OSError:
            pass

    def _select_index(i: int) -> None:
        # Move the cursor to visible[i] (clamped). In follow mode the pin
        # tracks the cursor. The viewport is realigned before draw().
        nonlocal selected_ip, followed_ip, followed_snapshot
        if not visible:
            return
        i = max(0, min(len(visible) - 1, i))
        selected_ip = visible[i]["ip"]
        if followed_ip:
            followed_ip       = selected_ip
            followed_snapshot = visible[i]

    while running:
        now = time.time()

        # auto-clear the snapshot flash-message after its timeout
        if snapshot_msg and now >= snapshot_msg_until:
            snapshot_msg = ""

        # a lone `g` waits GG_TIMEOUT for a second `g` (vim "gg" = jump to
        # the first row); when nothing follows it toggles /24 grouping.
        if pending_g and now - pending_g > GG_TIMEOUT:
            group_subnet = not group_subnet
            pending_g    = 0.0

        # ----- tick: collect data continuously; pause only freezes display -
        if now - last_tick >= interval:
            raw        = get_connections()
            active_ips = {c["ip"] for c in raw}

            prev_live = set(first_seen)          # last tick's live set (empty on the first tick)
            for c in raw:
                if c["ip"] not in first_seen:
                    first_seen[c["ip"]]        = now
                    baseline[c["ip"]]          = {"sent": c["sent"], "recv": c["recv"]}

            # Watchdog: one CSV row per transition, before stale cleanup.
            # "disconnect": live last tick, gone now (host/totals from
            # seen_snapshot, filled during the previous tick).
            # "connect"/"reconnect": new this tick; "reconnect" when the IP
            # was last seen within WATCHDOG_RECONNECT_SEC. The very first
            # tick only populates the live set and logs nothing. No flap
            # suppression — by design.
            if watchdog_enabled and watchdog_writer is not None:
                raw_by_ip = {c["ip"]: c for c in raw}
                for ip in sorted(prev_live - active_ips):
                    snap = seen_snapshot.get(ip, {})
                    _wd_event("disconnect", ip, snap.get("host", ip),
                              snap.get("sent", 0), snap.get("recv", 0))
                if last_tick:
                    for ip in sorted(active_ips - prev_live):
                        gone = (now - last_seen[ip]) if ip in last_seen else None
                        kind = ("reconnect" if gone is not None and gone <= WATCHDOG_RECONNECT_SEC
                                else "connect")
                        c = raw_by_ip[ip]
                        _wd_event(kind, ip, c["host"], c["sent"], c["recv"])

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

            # NFS-server-side per-client info — keyed by IP. The small `info`
            # files are always read (NFSv, CLIENT, KERNEL and the HOST
            # fallback need them); the large `states` files only when a
            # column or the active sort needs them.
            states_cols = ("mount", "opens", "locks", "delegs")
            need_states = (any(cols_visible.get(cid, True) for cid in states_cols)
                           or sort_key in states_cols)
            nfsd_info   = read_nfsd_clients_info(need_states=need_states)

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
                    "client":    nfsd_info.get(c["ip"], {}).get("client", ""),
                    "kernel":    nfsd_info.get(c["ip"], {}).get("kernel", ""),
                    "opens":     (nfsd_info.get(c["ip"], {}).get("states") or (0, 0, 0))[0],
                    "locks":     (nfsd_info.get(c["ip"], {}).get("states") or (0, 0, 0))[1],
                    "delegs":    (nfsd_info.get(c["ip"], {}).get("states") or (0, 0, 0))[2],
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
                snap["ghost"]     = True     # display-only row: excluded from totals
                seen_snapshot[ip] = snap

            # opt-in cap for the last-seen memory (0 = keep forever)
            if seen_max_hours > 0:
                prune_seen(last_seen, seen_snapshot, now - seen_max_hours * 3600)

            # server-wide nfsd counters → per-tick rates for the title row
            if server_stats_enabled:
                cur          = read_server_stats()
                server_rates = server_rates_from(server_prev, cur, now)
                server_prev  = {**cur, "ts": now}
            else:
                server_rates = {}
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
            # Both transitions also go to the watchdog log when it is on.
            alert_threshold_bps = alert_mb * 1024 * 1024
            for c in conns:
                ip = c["ip"]
                if c["activity"] >= alert_threshold_bps:
                    if ip not in alert_since:
                        alert_since[ip] = now
                    if now - alert_since[ip] >= alert_dur and not alert_active.get(ip):
                        alert_active[ip] = True
                        if watchdog_enabled:
                            _wd_event("alert", ip, c["host"], c["sent"], c["recv"])
                else:
                    alert_since.pop(ip, None)
                    if alert_active.pop(ip, None) and watchdog_enabled:
                        _wd_event("alert_clear", ip, c["host"], c["sent"], c["recv"])

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
                              if f in c["host"].lower() or f in c["ip"]
                          or f in c.get("client", "").lower()]
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
            if pending_g and key != ord("g"):      # any other key settles a lone `g`
                group_subnet = not group_subnet
                pending_g    = 0.0
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
                else:
                    # scroll the popup: ↑↓/PgUp/PgDn/Home/End/wheel vertically,
                    # ←/→ horizontally (long ss lines)
                    h, w = stdscr.getmaxyx()
                    g = text_popup_geometry(detail_text, DETAIL_TITLE, h, w)
                    dy = {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                          curses.KEY_PPAGE: -g["page"], curses.KEY_NPAGE: g["page"]}.get(key, 0)
                    dx = {curses.KEY_LEFT: -DETAIL_HSTEP, curses.KEY_RIGHT: DETAIL_HSTEP}.get(key, 0)
                    if key == curses.KEY_MOUSE:
                        ev = _mouse_event()
                        if ev and ev[0] & MOUSE_WHEEL_UP:
                            dy = -MOUSE_STEP
                        elif ev and ev[0] & MOUSE_WHEEL_DOWN:
                            dy = MOUSE_STEP
                    if key == curses.KEY_HOME:
                        detail_y = detail_x = 0
                    elif key == curses.KEY_END:
                        detail_y = g["max_y"]
                    else:
                        detail_y = max(0, min(detail_y + dy, g["max_y"]))
                        detail_x = max(0, min(detail_x + dx, g["max_x"]))
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
                            elif edit_field == "seen_max" and 0 <= v <= 9999:
                                seen_max_hours = v
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
                                       colors=base_color_overrides,
                                       bright_sort_col=bright_sort_col,
                                       show_seen_clients=show_seen_clients,
                                       watchdog_enabled=watchdog_enabled,
                                       watchdog_path=watchdog_path,
                                       spark_len=spark_len,
                                       mouse=mouse_enabled,
                                       server_stats=server_stats_enabled,
                                       seen_max_hours=seen_max_hours):
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
                                       colors=base_color_overrides,
                                       bright_sort_col=bright_sort_col,
                                       show_seen_clients=show_seen_clients,
                                       watchdog_enabled=watchdog_enabled,
                                       watchdog_path=watchdog_path,
                                       spark_len=spark_len,
                                       mouse=mouse_enabled,
                                       server_stats=server_stats_enabled,
                                       seen_max_hours=seen_max_hours):
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
                        elif item[0] == "mouse":
                            mouse_enabled = not mouse_enabled
                            _apply_mouse(mouse_enabled)
                        elif item[0] == "sstats":
                            server_stats_enabled = not server_stats_enabled
                            if not server_stats_enabled:
                                server_rates, server_prev = {}, {}
                        elif item[0] == "seen_max":
                            edit_field = "seen_max"
                            edit_input = str(seen_max_hours)
                        elif item[0] == "spark_len":
                            edit_field = "spark_len"
                            edit_input = str(spark_len)
                elif key == ord("v"):                                  # toggle sort dir from popup
                    sort_rev = not sort_rev
            elif show_help and key in (curses.KEY_UP, curses.KEY_DOWN,
                                       curses.KEY_PPAGE, curses.KEY_NPAGE,
                                       curses.KEY_HOME, curses.KEY_END,
                                       curses.KEY_MOUSE):
                # while the help is open these scroll the help, not the table
                h, w = stdscr.getmaxyx()
                step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                        curses.KEY_PPAGE: -PAGE_STEP,
                        curses.KEY_NPAGE: PAGE_STEP}.get(key, 0)
                if key == curses.KEY_MOUSE:
                    ev = _mouse_event()
                    if ev and ev[0] & MOUSE_WHEEL_UP:
                        step = -MOUSE_STEP
                    elif ev and ev[0] & MOUSE_WHEEL_DOWN:
                        step = MOUSE_STEP
                if key == curses.KEY_HOME:
                    help_scroll = 0
                elif key == curses.KEY_END:
                    help_scroll = help_max_scroll(h, w)
                else:
                    help_scroll = max(0, min(help_scroll + step, help_max_scroll(h, w)))
            elif key == 27 and show_help:                              # Esc closes help
                show_help = False
            elif key == ord("q"):
                running = False
                break
            elif key == ord("h"):
                show_help   = not show_help
                help_scroll = 0
            elif key == ord("/"):
                filter_saved        = filter_str
                filter_input_active = True
                show_help           = False
            elif key == ord("g"):
                if pending_g and now - pending_g <= GG_TIMEOUT:   # gg → first row
                    pending_g = 0.0
                    _select_index(0)
                else:
                    pending_g = now
            elif key == ord("i"):
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
                         curses.KEY_PPAGE, curses.KEY_NPAGE,
                         curses.KEY_HOME, curses.KEY_END) and visible:
                if key == curses.KEY_HOME:
                    _select_index(0)
                elif key == curses.KEY_END:
                    _select_index(len(visible) - 1)
                else:
                    _move_cursor({curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                                  curses.KEY_PPAGE: -PAGE_STEP,
                                  curses.KEY_NPAGE: PAGE_STEP}[key])
            elif key == curses.KEY_MOUSE:
                ev = _mouse_event()
                if ev and visible:
                    bstate, mx, my = ev
                    if bstate & MOUSE_WHEEL_UP:
                        _move_cursor(-MOUSE_STEP)
                    elif bstate & MOUSE_WHEEL_DOWN:
                        _move_cursor(MOUSE_STEP)
                    elif bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                        i = scroll_top + (my - FIRST_DATA_ROW)
                        if (FIRST_DATA_ROW <= my < FIRST_DATA_ROW + _viewport_rows()
                                and 0 <= i < len(visible)):
                            _select_index(i)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                # horizontal column scrolling; clamped against the column
                # count before draw()
                col_offset = max(0, col_offset + (1 if key == curses.KEY_RIGHT else -1))
            elif key == ord("G") and visible:          # vim: jump to last row
                _select_index(len(visible) - 1)
            elif key in (10, 13, curses.KEY_ENTER) and visible:
                target = selected_ip if selected_ip in [c["ip"] for c in visible] else visible[0]["ip"]
                detail_text = fetch_detail(target)
                detail_y = detail_x = 0
                selected_ip = target
                break                                                  # popup opened; stop draining
            elif key == ord("V"):                      # ACTIVITY, idle first
                sort_key  = "activity"
                sort_rev  = False                  # idle first
                show_help = False
            elif 0 <= key < 256 and chr(key) in KEY_MAP:
                sort_key  = KEY_MAP[chr(key)]
                sort_rev  = True
                show_help = False
            elif key == ord("v"):
                sort_rev = not sort_rev
            elif key == ord("d"):
                # Snapshot dump: write the current visible view as plain
                # text. Footer flashes a confirmation/error for
                # SNAPSHOT_TIMEOUT seconds.
                active_cols = _build_active_cols(cols_visible, show_ip_in_host, spark_len)
                snap_path   = snapshot_path(now)
                try:
                    write_snapshot(snap_path, visible, len(display_source),
                                   active_cols, sort_key, sort_rev,
                                   filter_str, group_subnet,
                                   followed_ip, paused, csv_enabled,
                                   server_stats_text(server_rates))
                    snapshot_msg       = f"snapshot saved → {snap_path}"
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

        # ----- viewport: clamp the offsets, keep the cursor on screen -------
        n_cols     = len(_build_active_cols(cols_visible, show_ip_in_host, spark_len))
        col_offset = max(0, min(col_offset, n_cols - 2))
        page_rows  = _viewport_rows()
        scroll_top = max(0, min(scroll_top, len(visible) - page_rows))
        if selected_ip:
            for i, c in enumerate(visible):
                if c["ip"] == selected_ip:
                    if i < scroll_top:
                        scroll_top = i
                    elif i >= scroll_top + page_rows:
                        scroll_top = i - page_rows + 1
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
             spark_len,
             scroll_top=scroll_top,
             col_offset=col_offset,
             help_scroll=help_scroll,
             mouse_enabled=mouse_enabled,
             server_line=server_stats_text(server_rates),
             server_warn=server_stats_warn(server_rates),
             server_stats_enabled=server_stats_enabled,
             seen_max_hours=seen_max_hours,
             detail_y=detail_y,
             detail_x=detail_x)
        time.sleep(0.1)


def _int_in_range(lo: int, hi: int):
    def conv(s: str) -> int:
        v = int(s)
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return v
    return conv


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
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
    p.add_argument("--port", type=_int_in_range(1, 65535), metavar="N",
                   help="TCP port to watch; overrides the config (default 2049)")
    p.add_argument("--interval", type=_int_in_range(1, 9999), metavar="SEC",
                   help="tick interval in seconds; overrides the config (default 2)")
    p.add_argument("--config", metavar="FILE",
                   help="user config to read and save instead of ~/.config/nfsmon.conf")
    return p.parse_args(argv)


if __name__ == "__main__":
    _args = _parse_args()
    if _args.config:
        CONFIG_USER = os.path.expanduser(_args.config)
    if _args.port is not None:
        CLI_OVERRIDES["port"] = _args.port
    if _args.interval is not None:
        CLI_OVERRIDES["interval"] = _args.interval
    os.environ.setdefault("ESCDELAY", str(ESC_DELAY_MS))   # read by ncurses at initscr()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
