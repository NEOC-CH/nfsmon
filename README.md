# nfsmon — NFSv4 Server-side Traffic Monitor

A curses TUI for watching NFSv4 traffic on the **server**. Shows, per
connected client, live: total bytes + delta since start, B/s rates,
connection count, connection age, NFS version, mounted path, TCP RTT,
sparkline trend, and how long ago a client was last seen. Includes
filtering, sorting, pause, follow, /24 subnet aggregation, CSV logging,
alerts, watchdog (disconnect logging), themes (8-color base + 256-color
aliases), and persistent configuration.

```
NFS Traffic  port:2049  14:32:07  interval:2s  since:09:14:11  (05:17:56)
 HOST                            CONNS    CONNECTED        SENT       ΔSENT     B/s SENT
 vp-app-01.xxx.tld                   8     04:21:33      4.2 GiB    1.1 GiB   1.4 MiB/s
 vp-app-02.xxx.tld                   8     04:18:02      3.8 GiB  892.3 MiB   2.1 MiB/s
 vp-batch-03.xxx.tld                 4     03:55:11   821.4 MiB  120.5 MiB  340.2 KiB/s
 ...
 clients:14  total conns:48  sent:18.4 GiB  recv:6.7 GiB    sort:sent↓  /:filter  o:opts ...
```

---

## Contents

- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Usage](#usage)
- [TUI layout](#tui-layout)
- [Hotkeys (full reference)](#hotkeys-full-reference)
- [The o-popup (Options)](#the-o-popup-options)
- [Column reference](#column-reference)
- [Sorting](#sorting)
- [Filtering and grouping](#filtering-and-grouping)
- [Selection, follow, and ghost row](#selection-follow-and-ghost-row)
- [Last-seen clients](#last-seen-clients)
- [Bright sorted column](#bright-sorted-column)
- [Snapshot dumps](#snapshot-dumps)
- [CSV logging](#csv-logging)
- [Alerts](#alerts)
- [Watchdog (disconnect logging)](#watchdog-disconnect-logging)
- [Configurable TREND length](#configurable-trend-length)
- [Themes and colors](#themes-and-colors)
- [Configuration files](#configuration-files)
- [Architecture and data sources](#architecture-and-data-sources)
- [Permissions](#permissions)
- [Compatibility](#compatibility)
- [Known limitations](#known-limitations)
- [Version and author](#version-and-author)

---

## Prerequisites

- Linux kernel ≥ 5.3 (for the `ss -ti` RTT stats; on older kernels the
  RTT column stays empty, everything else still works)
- Python ≥ 3.8 with the `curses` module (standard on every distro Python)
- `iproute2` (the `ss` binary) on `PATH`
- Read access to `/proc/fs/nfsd/clients/*/{info,states}` for the NFSv
  version + mount path detection — i.e. run as `root` or with the
  equivalent capabilities
- A terminal with ≥ 8 colors (better: `$TERM` contains `-256color` so
  the 256-color aliases and themes render correctly)

Not required: pip packages — everything is from the standard library.

---

## Installation

`nfsmon` is a single Python script. Three steps:

```bash
# 1. Copy the script to the server (or via scp / Ansible)
sudo cp scripts/nfsmon.py /usr/local/bin/nfsmon
sudo chmod +x /usr/local/bin/nfsmon

# 2. Optional: system-wide defaults
sudo cp scripts/nfsmon.conf.example /etc/nfsmon.conf

# 3. Optional: per-user theme collection
mkdir -p ~/.config/nfsmon/colors
cp scripts/themes/*.conf ~/.config/nfsmon/colors/
```

Without steps 2 + 3 the tool starts with built-in defaults — themes
can still be selected from the popup as soon as files exist in
`~/.config/nfsmon/colors/`.

---

## Usage

```bash
sudo nfsmon                # start the tool (curses TUI)
nfsmon --version           # print version + author and exit
nfsmon -V                  # short form
nfsmon --help              # argparse help
```

Quit anytime with `q` or `Ctrl-C`.

---

## TUI layout

```
┌──────────────────────────────────────────────────────────────────┐
│ NFS Traffic  port:2049  14:32:07  interval:2s  since:09:14:11 ...│  ← Title row
│ HOST           CONNS  CONNECTED  SENT  ΔSENT  B/s SENT  ...      │  ← Header (bold + underline)
│ vp-app-01.xxx.tld ...                                            │  ← Data rows (one per client)
│ ...                                                              │
│ vp-stale-99.xxx.tld ...                                10m 5s    │  ← Ghost rows (last-seen)
│                                                                  │
│ clients:14  total conns:48  ...  sort:sent↓  /:filter  o:opts ...│  ← Footer (status + hint)
└──────────────────────────────────────────────────────────────────┘
```

- **Title row** — port, current time, tick interval, program start time
- **Header row** — active columns in their on-screen order; HOST flips to IP when the `Shift+i` toggle is active
- **Data rows** — one per live NFS client; each row color-coded by throughput tier (idle / low / medium / high / alert)
- **Ghost rows** — only when `Show last-seen clients` is on: clients that were once connected but aren't anymore — always below the live rows
- **Footer** — totals on the left (clients/conns/sent/recv), status + hotkey hint on the right

Throughput color tiers (with the default theme):
- **idle** — no activity (dimmed)
- **low** — < 1 MB/s (green)
- **medium** — 1–10 MB/s (yellow, bold)
- **high** — ≥ 10 MB/s (red, bold)
- **alert** — over threshold for ≥ duration (red, bold, blink)

---

## Hotkeys (full reference)

Also reachable in the tool via `h`.

### Sort

| Key       | Effect                                             |
| --------- | -------------------------------------------------- |
| `s`       | Sort by **SENT** (bytes total)                     |
| `Shift+s` | Sort by **ΔSENT** (bytes since baseline)           |
| `r`       | Sort by **RECV** (total)                           |
| `Shift+r` | Sort by **ΔRECV**                                  |
| `b`       | Sort by **B/s SENT** (live rate)                   |
| `Shift+b` | Sort by **B/s RECV**                               |
| `c`       | Sort by **CONNS**                                  |
| `a`       | Sort by **CONNECTED** (connection age)             |
| `Shift+a` | Sort by **ACTIVITY** (idle first)                  |
| `i`       | Sort by **IP**                                     |
| `Shift+h` | Sort by **HOST** (hostname)                        |
| `n`       | Sort by **NFSv**                                   |
| `m`       | Sort by **MOUNT**                                  |
| `t`       | Sort by **RTT** (latency)                          |
| `l`       | Sort by **SEEN** (last seen)                       |
| `v`       | Toggle sort direction (↑/↓)                        |

`TREND` (sparkline) intentionally has no hotkey — sparkline strings
aren't meaningfully comparable.

### View

| Key             | Effect                                     |
| --------------- | ------------------------------------------ |
| `/`             | Filter by host or IP substring             |
| `↑` / `↓`       | Move selection cursor ±1                   |
| `PgUp` / `PgDn` | Move selection ±5 rows                     |
| `Enter`         | Detail popup for the selected row          |
| `f`             | Follow mode (pin selection to an IP)       |
| `g`             | Group by /24 subnet                        |
| `Shift+i`       | HOST column: hostname ↔ IP toggle          |

### Actions

| Key        | Effect                                     |
| ---------- | ------------------------------------------ |
| `o`        | Open Options popup                         |
| `Shift+d`  | Snapshot dump → `/tmp/nfsmon_snapshot.txt` |
| `space`    | Pause / resume the display                 |
| `z`        | Baseline reset (zero Δ + connection age)   |

### General

| Key   | Effect                                 |
| ----- | -------------------------------------- |
| `h`   | Toggle help popup                      |
| `q`   | Quit                                   |
| `Esc` | Close popup / cancel input             |

---

## The o-popup (Options)

Three tabs, layout 70×40 (or smaller, scaled to terminal size).
Navigation: `←`/`→` switches tabs (in the main area), `↑`/`↓` selects
rows, `space` toggles/activates, `Tab` jumps to the
**[Save Settings]** button at the bottom-right.

```
┌────── Options ─────────────────────────────────────────────────────┐
│ [defaults] [columns] [sort]                                        │
│                                                                    │
│  Interval (s):  [   2]                                             │
│  Port:          [ 2049]                                            │
│  CSV Log:       [ ]  /var/log/nfsmon.csv                           │
│  Alert MB/s:    [  10]                                             │
│  Alert duration (s): [   5]                                        │
│  Theme:  ‹ default ›  (space to cycle)                             │
│  Bright sorted column:  [x]                                        │
│  Show last-seen clients:  [ ]                                      │
│  Watchdog (log disconnects):  [ ]  /var/log/nfsmon-events.log      │
│  TREND length:  [10]  (10-40)                                      │
│                                                                    │
│ Tab:save  ←→:tabs  ↑↓:sel  space:toggle  Esc:close   [Save Settings]│
└────────────────────────────────────────────────────────────────────┘
```

### Tab `[defaults]`

Per-program settings:

- **Interval (s)** — how often (in s) data is refreshed. Range 1–9999.
- **Port** — which TCP port to filter as the "NFS server port". Default 2049.
- **CSV Log** — toggle. When active, one row per client is appended each tick to `/var/log/nfsmon.csv` (or the configured path).
- **Alert MB/s** — throughput threshold above which the alert logic fires.
- **Alert duration (s)** — how long a client must stay above the threshold before being marked as alerting.
- **Theme** — active theme. `space` cycles through `(default)` + every file in `~/.config/nfsmon/colors/`. The change is visible immediately.
- **Bright sorted column** — toggle. The active sort column is rendered in its bright variant (A_BOLD, A_DIM stripped).
- **Show last-seen clients** — toggle. Previously-seen clients show up below the live ones as ghost rows.
- **Watchdog (log disconnects)** — toggle. Writes one CSV line per disconnect event to `/var/log/nfsmon-events.log` (or the configured path). Path comes from `[watchdog] path` in the config.
- **TREND length** — numeric edit (10-40). Controls both the column width of the TREND column and the length of the per-IP sample buffer (`rate_history`). Default 10, max 40 — values out of range are clamped to the default at startup.

### Tab `[columns]`

One toggle row per column from `COLUMNS`. `[x]` = visible, `[ ]` = hidden. `space` flips it.

### Tab `[sort]`

One radio row per column plus `IP` and `ACTIVITY (idle first w/ v)`. `space` sets `sort_key` to the highlighted entry. The hotkeys (s/r/b/...) are direct shortcuts for exactly this.

### `[Save Settings]` button

Bottom-right of every tab. `Tab` toggles between the main area and the button:
- `Enter` on the button → save + close popup
- `space` on the button → save + popup stays open

Always saves to `~/.config/nfsmon.conf`. `/etc/nfsmon.conf` is **never** overwritten.

---

## Column reference

| Column      | Default | Content                                                                  |
| ----------- | ------- | ------------------------------------------------------------------------ |
| `HOST`      | on      | DNS name (reverse lookup), or IP when `Shift+i` is active                |
| `NFSv`      | off     | Negotiated NFS version (`4.2`, `4.1`, ...) from `/proc/fs/nfsd/clients/*/info` |
| `MOUNT`     | off     | Longest common path prefix of the client's mounted paths                 |
| `RTT`       | off     | TCP RTT (averaged over all active connections, in ms) from `ss -ti`      |
| `CONNS`     | on      | Number of active TCP connections from this client                        |
| `CONNECTED` | on      | How long the client has been continuously connected (`HH:MM:SS` or `Xd HH:MM:SS`) |
| `SEEN`      | off     | How long ago the client was last live (`now`, `15s`, `5m 30s`, `1h`, `3d`). For live clients: `now`. For ghost clients: time since the last tick they showed up in `ss` |
| `SENT`      | on      | Bytes the server has sent to the client (total, since the last tcp-stat reset) |
| `ΔSENT`     | on      | Difference vs. baseline (program start or `z`)                           |
| `B/s SENT`  | off     | Current sending rate (bytes per second)                                  |
| `RECV`      | on      | Bytes received from the client (total)                                   |
| `ΔRECV`     | on      | Difference vs. baseline                                                  |
| `B/s RECV`  | off     | Current receiving rate                                                   |
| `TREND`     | off     | Sparkline of the combined rate over the last N ticks. Width N is configurable via `[defaults] → TREND length` (range 10-40, default 10) |

---

## Sorting

- `sort_key` is the current sort key, `sort_rev` the direction.
- Hotkeys set `sort_key` and `sort_rev=True` (descending).
- `Shift+a` is the special case: `sort_key="activity"`, `sort_rev=False` — idle clients first.
- `v` toggles only the direction (`sort_rev`), keeping the current key.
- Footer shows the current sort as `sort:<key>↓` or `sort:<key>↑`.

With **Bright sorted column** active, the column corresponding to the
sort is also re-rendered per row with `(attr & ~A_DIM) | A_BOLD` —
effectively the bright variant of the role.

---

## Filtering and grouping

- `/` opens an input-line mode in the footer. The substring is matched
  against `host` and `ip` (lowercase). `Esc` discards, `Enter` applies.
- While filtering, the cursor stays visible (`/<text>_`) and the footer
  shows `Enter:apply  Esc:cancel`.
- An active filter shows up in the hint as `filter:<text>`. Open the
  filter again with `/`; an empty string clears it.
- `g` toggles **group by /24**. IPs are bucketed into `10.0.10.0/24`
  groups; numeric fields are summed, `age` is the oldest `first_seen`
  in the group. `RTT` stays empty (aggregation isn't meaningful).

Filter and grouping apply to live rows. For ghost rows (last-seen):
filter is applied, grouping is not — ghosts stay individually
identifiable at the bottom.

---

## Selection, follow, and ghost row

- `↑`/`↓`/`PgUp`/`PgDn` set `selected_ip`. The selection is marked with
  `A_REVERSE`.
- The selection auto-hides after 5 s of no input (except in follow
  mode).
- `Enter` on the selection opens a detail popup with extra per-client
  info (NFSv, mount, etc., depending on what `fetch_detail` returns).
- `f` toggles **follow**: the selection is pinned to the current IP
  (no auto-hide, no drop on disconnect). When the followed IP falls
  out of the visible set, its last snapshot row is rendered as a
  **ghost row** at the bottom (dimmed + reversed).

Note: this follow-ghost row is independent of the **last-seen** feature
(see next section). The follow-ghost is *one* IP you're explicitly
following. Last-seen ghosts are *all* IPs that have ever connected.

---

## Last-seen clients

Toggle: `o` → `[defaults]` → `Show last-seen clients` → `space`.

When active, clients that previously appeared in `ss` but no longer do
are shown as additional rows **below** the live clients — with all their
last-known values (HOST, SENT, RECV, ΔSENT, ΔRECV) and `B/s = 0`,
`CONNS = 0`, `activity = 0`. The `SEEN` column shows how long ago that
was (`now`, `15s`, `5m 30s`, `1h 25m`, `3d 5h`).

Two state variables hold this in memory:

- `last_seen: Dict[str, float]` — IP → timestamp of latest appearance
- `seen_snapshot: Dict[str, Dict]` — IP → last concrete data snapshot

Both are updated each tick for every live IP. The stale-IP cleanup in
the tick (which drops `first_seen`, `baseline`, `prev_totals` for IPs
no longer live) **deliberately does not touch these two**.

Sorting: live rows are sorted by `sort_key`; ghost rows are *also*
sorted by `sort_key` — but the ghost block always stays **below** the
live block, regardless of direction.

`l` is the direct hotkey for "Sort by SEEN".

**Memory**: `last_seen`/`seen_snapshot` grow unbounded (one entry per
IP we've ever seen). That's intentional — once seen, an IP stays
discoverable. In practice you get a few dozen to a few hundred IPs
per day; even after weeks that's only a few MB. If you ever need a
cap (e.g. only the last 24 h), add a cleanup loop every N ticks.

---

## Bright sorted column

Toggle: `o` → `[defaults]` → `Bright sorted column` → `space`.

When active, the currently active sort column is re-rendered per data
row with:

```python
bright_attr = (attr & ~curses.A_DIM) | curses.A_BOLD
```

Effect: `A_DIM` is stripped, `A_BOLD` is added — on most terminals the
column appears in the bright variant of its role. Idle rows are lifted
out of the dimmed tone into full intensity; color tiers move to their
bright versions.

Special case: when `Shift+i` is active (HOST column shows IP) and you
sort by `i`/IP, the HOST column (which is currently displaying IP) is
treated as the "active sort column".

---

## Snapshot dumps

`Shift+d` writes a plain-text dump of the currently visible view to
`/tmp/nfsmon_snapshot.txt`. The content mirrors exactly what's on
screen — same filter, same sort, same columns, same IP/HOST display.

Use case: pinning the current state without taking a screenshot, e.g.
to attach to tickets, emails, bug reports. The footer flashes a
confirmation with the file path for 3 s.

---

## CSV logging

Toggle: `o` → `[defaults]` → `CSV Log` → `space`. Default: off.

When on, the tool writes **one row per client per tick** to
`/var/log/nfsmon.csv` (or the path set in the config). Format:

```csv
timestamp,ip,host,sent,recv,dsent,drecv,rate_sent,rate_recv,conns
2026-05-05T14:32:07,10.0.10.42,vp-app-01.xxx.tld,4523846542,1284567890,1182937462,403215100,1456823.45,512311.10,8
```

- Header is only written when the file is freshly created or empty
- `f.flush()` after every tick (no data loss on crash, but more disk writes)
- OSError on write (full disk, permission denied) → toggle stays on, writes silently fail

Analysis e.g. with `pandas`:

```python
import pandas as pd
df = pd.read_csv("/var/log/nfsmon.csv", parse_dates=["timestamp"])
df.groupby("ip")["rate_sent"].mean().sort_values(ascending=False).head(10)
```

---

## Alerts

Trigger: per client, `activity = rate_sent + rate_recv` is measured
each tick. If it stays ≥ `alert_mb * 1024 * 1024` (B/s) for at least
`alert_dur` seconds, the client is added to `alert_active`.

- The data row is rendered with the `alert` role (default red, bold, blink)
- The footer shows `ALERT:<n>` — how many clients are currently alerting
- As soon as the rate drops below the threshold, the alert is reset

Thresholds are editable in the popup (`Alert MB/s`, `Alert duration (s)`)
and persisted via the config (`[alerts]` section: `threshold_mb`,
`duration_sec`).

---

## Watchdog (disconnect logging)

Toggle: `o` → `[defaults]` → `Watchdog (log disconnects)` → `space`. Default: off.

When on, every **disconnect** is written as a CSV line into an event
log file (default `/var/log/nfsmon-events.log`, path configurable via
`[watchdog] path`). Format:

```csv
timestamp,event,ip,host,last_sent,last_recv
2026-05-05T14:32:07,disconnect,10.0.10.42,vp-app-01.xxx.tld,4523846542,1284567890
```

**Detection**: each tick, *before* the stale-IP cleanup, the tick block
computes `set(first_seen.keys()) - active_ips`. IPs that were live in
the previous tick and aren't anymore → disconnect. One row per IP;
host + last_sent + last_recv come from `seen_snapshot` (what the
previous tick stored).

**Intentional limitations**:
- Disconnects only, no reconnects. If you need the inverse (e.g.
  "IP X was gone for Y seconds and is back"), it's available via
  cross-reference between `last_seen` (timestamp in seen_snapshot)
  and the current live list — reconnect detection would be a
  ~10-line addition.
- No "flap filter". If a client bounces every tick, every disappearance
  writes a separate line. If needed: add a threshold/suppression
  layer.

**Toggle semantics**:
- Toggle-off closes the file handle.
- Toggle-on opens via `_open_watchdog()` (header for new/empty files,
  append otherwise). At startup: if config says `watchdog_enabled = true`,
  the file is opened automatically — silent fail on OSError, the toggle
  stays OFF in that case.

**Analysis** e.g. with `awk` or `pandas`:

```bash
# Top 10 hosts by number of disconnects in the last 24 h
awk -F, -v cutoff="$(date -d '24 hours ago' -u +%Y-%m-%dT%H:%M:%S)" \
    '$1 > cutoff {print $4}' /var/log/nfsmon-events.log \
    | sort | uniq -c | sort -rn | head
```

---

## Configurable TREND length

Edit field: `o` → `[defaults]` → `TREND length` → `space` → digits → Enter.
Range **10–40**, default 10.

Sets both the **column width** of the TREND column and the **buffer
length** of the per-IP sample stack (`rate_history`). Both share the
same value (`spark_len`) so the column shows exactly as many samples
as it tracks.

**Mechanics**:
- `_build_active_cols(cols_visible, show_ip_in_host, spark_len)`
  substitutes the TREND column width (single source of truth for the
  renderer + `write_snapshot`).
- `sparkline(values, length=spark_len)` renders that many bars.
- Tick logic: `if len(h) > spark_len: del h[: len(h) - spark_len]`.
- When growing 10 → 40, it takes N-1 ticks for the new buffer to
  fill; until then you see leading whitespace — by design.

**Persistence**: `[general] spark_len = N` in `~/.config/nfsmon.conf`.
Out-of-range values from the config are clamped to the default at
`main()` startup; no crash.

**Caveat**: on a small terminal with many columns enabled +
`spark_len=40`, horizontal space may be tight — columns at the right
edge get cropped (`safe_addstr` swallows it; no crash, but information
is lost).

---

## Themes and colors

A theme is a `*.conf` file with a `[colors]` section in
`~/.config/nfsmon/colors/`. Selectable in the tool via `o` →
`[defaults]` → `Theme:` → `space` cycling. The built-in default is
called `(default)` and lives in the code (`DEFAULT_COLORS` in
`nfsmon.py`).

### 9 roles

| Role              | Where it appears                                                | Default            |
| ----------------- | --------------------------------------------------------------- | ------------------ |
| `title`           | Title row + popup borders + popup titles                        | `cyan,bold`        |
| `key_bar`         | Tab bar in the o-popup, save button (focused), active tab       | `yellow,bold`      |
| `text`            | Popup body text, inactive tabs (with dim)                       | `green`            |
| `footer`          | Bottom status row + help headers                                | `cyan`             |
| `alert`           | Row whose throughput exceeds the alert threshold                | `red,bold,blink`   |
| `idle`            | Rows with no throughput, ghost row                              | `white,dim`        |
| `activity_low`    | < 1 MB/s                                                        | `green`            |
| `activity_medium` | 1–10 MB/s                                                       | `yellow,bold`      |
| `activity_high`   | ≥ 10 MB/s                                                       | `red,bold`         |

### Per-entry format

```
<role> = <fg_color>[,<attr>[,<attr>...]]
```

`<fg_color>` is one of:
- a name from the 8-color base (`black`, `red`, `green`, ...)
- a 256-color alias (`orange`, `pink`, `gold`, `mint`, `darkgray`, ...)
- a numeric code (e.g. `208`, `34`)

`<attr>` values are comma-separated: `bold`, `dim`, `blink`, `reverse`,
`underline`, `standout`, `normal`.

Full list: see `color.md` next to this file.

### Bundled themes

In `scripts/themes/` (drop-in copy):

- `default.conf` — built-in defaults made explicit (8-color base)
- `mono.conf` — no colors, attributes only (for 8-color terminals or screen sharing)
- `nord.conf` — cool nordic blue/cyan
- `dracula.conf` — purple/pink/cyan on dark
- `gruvbox-dark.conf` — warm yellow/orange
- `tokyo-night.conf` — deep blue with cyan/magenta
- `monokai.conf` — magenta/green/yellow classic
- `matrix.conf` — all green, graded 22 → 46
- `amber-mono.conf` — amber CRT, graded 130 → 220
- `cyberpunk.conf` — neon magenta/cyan/pink
- `solarized-dark.conf` — official Solarized codes

### Theme precedence (lowest to highest)

1. `DEFAULT_COLORS` (in code)
2. `[colors]` from `/etc/nfsmon.conf`
3. `[colors]` from `~/.config/nfsmon.conf`
4. `[colors]` from `~/.config/nfsmon/colors/<theme>.conf` (when `theme` is set)

Themes override the user config. If you want custom colors
independent of any theme, put them in `[colors]` of `nfsmon.conf` and
leave `theme =` empty.

### 256-color fallback

On terminals where `curses.COLORS < 256`, codes ≥ `COLORS` are
silently reverted per role to the role's built-in default. If the
default also doesn't fit, it falls back to `-1` (terminal default fg).
No crash, no warning — just less color.

---

## Configuration files

### Lookup order

1. `/etc/nfsmon.conf` — system defaults
2. `~/.config/nfsmon.conf` — user override (wins on duplicate keys)

`save_config` **always** writes to `~/.config/nfsmon.conf` —
`/etc/nfsmon.conf` is never overwritten by the tool.

### Format

INI via Python's `configparser`. Full example file:
`scripts/nfsmon.conf.example`. The relevant sections:

```ini
[general]
interval          = 2
port              = 2049
group_subnet      = false
show_ip           = false
sort_key          = sent
sort_rev          = true
theme             = nord
bright_sort_col   = true
show_seen_clients = false
watchdog_enabled  = false
spark_len         = 10

[csv]
enabled = false
path    = /var/log/nfsmon.csv

[columns]
host      = true
nfsv      = false
mount     = false
rtt_avg   = false
last_seen = false
conns     = true
age       = true
sent      = true
dsent     = true
rate_sent = false
recv      = true
drecv     = true
rate_recv = false
spark     = false

[alerts]
threshold_mb = 10
duration_sec = 5

[watchdog]
path = /var/log/nfsmon-events.log

[colors]
title           = cyan,bold
key_bar         = yellow,bold
text            = green
footer          = cyan
alert           = red,bold,blink
idle            = white,dim
activity_low    = green
activity_medium = yellow,bold
activity_high   = red,bold
```

### Theme file (sub-schema)

```ini
# ~/.config/nfsmon/colors/<theme>.conf
[colors]
title           = 33,bold
key_bar         = 37,bold
# ... more roles
```

Only the `[colors]` section is read. Other sections in a theme file
are ignored.

---

## Architecture and data sources

`nfsmon` is a single Python script with no external pip dependencies.
Per-tick data sources:

1. **`ss --no-header --tcp -tinp dport == :<port>`**
   - Per connection: source IP/port, bytes-sent, bytes-recv,
     `tcpi_rtt` from the `-i` flag (RTT in ms)
   - Aggregation per source IP: connections summed, bytes summed,
     RTT averaged

2. **`/proc/fs/nfsd/clients/<id>/info`** (kernel ≥ 5.3)
   - Per client: `address: "<ip>:<port>"`, `minor version: <n>`
   - Yields the NFS version → `c["nfsv"]`

3. **`/proc/fs/nfsd/clients/<id>/states`**
   - Per client: all mounted paths (as `"…"` quoted strings)
   - `nfsmon` extracts the longest common directory prefix → `c["mount"]`

4. **`socket.gethostbyaddr(ip)`** (in a daemon thread, cached)
   - Reverse DNS for `c["host"]`. On failure: shows the IP string.

Tick order:
1. Parse ss output → `raw`
2. Set `first_seen`, `baseline` for new IPs
3. Stale cleanup: drop IPs no longer active from `first_seen`,
   `baseline`, `prev_totals`, `rate_history`, `alert_since`,
   `alert_active` (but **not** from `last_seen` / `seen_snapshot`)
4. Compute per-IP rates (delta vs. previous tick / dt)
5. Advance the per-IP sparkline history
6. Read NFSd info from `/proc/fs/nfsd` (NFSv + mount)
7. Merge everything into the `conns` list
8. `last_seen[ip] = now` + `seen_snapshot[ip] = ...` for every live IP
9. Write a CSV row per client (when enabled)
10. Alert detection (threshold/duration)

UI loop (every 100 ms):
- Drain + process keys
- Compute the visible list (filter/group/sort + ghost append)
- `draw()` to redraw

---

## Permissions

`nfsmon` needs:

- **Root or equivalent** for `/proc/fs/nfsd/clients/`. Without that
  access, `NFSv` and `MOUNT` stay empty — the rest still works.
- `/var/log/nfsmon.csv` writable (when CSV logging is on). The default
  path is root-only writable; for non-root, configure a path under `$HOME`.
- `/var/log/nfsmon-events.log` writable (when watchdog is on). Default
  path is root-only too; redirect via `[watchdog] path` to e.g.
  `~/nfsmon-events.log`.
- `/tmp/nfsmon_snapshot.txt` writable (for the `Shift+d` dump). The
  standard `/tmp` is fine.
- `~/.config/nfsmon/` writable (for persistent config).

---

## Compatibility

| Component | Requirement | Behavior on violation |
| --------- | ----------- | --------------------- |
| Linux kernel | ≥ 5.3 for RTT | Older kernels: RTT column empty, otherwise OK |
| Linux kernel | ≥ 5.3 for `/proc/fs/nfsd/clients/` | Older: NFSv/MOUNT empty |
| Python | ≥ 3.8 | Older: f-strings + type hints don't compile |
| `ss` binary | iproute2 present | Missing: tool starts but shows 0 connections |
| Terminal colors | ≥ 8 | Fewer: themes fall back to `-1` (default fg) |
| Terminal colors | < 256 | Codes ≥ COLORS revert per role to the built-in default |
| `nconnect` (client) | optional | More CONNS per client when active (≥ 5.3 client kernel) |

---

## Known limitations

- **NFSv3 clients are not shown.** `nfsmon` filters by default on
  `tcp dport == 2049` (the standard NFSv4 port); v3 runs over
  rpcbind/portmap with dynamic ports and isn't picked up. Workaround:
  set `port` in the popup to the v3 port.
- **`last_seen` / `seen_snapshot` grow unbounded.** One entry per IP
  ever seen. Practically harmless (KB range), but on servers with
  thousands of distinct IPs per day this might matter. A cleanup pass
  would have to be added.
- **Group-by /24 + last-seen clients**: ghost rows are deliberately
  **not** grouped. That's intentional (identifiability), but it means
  with /24 aggregation the live block is denser while the full ghost
  block hangs unaggregated below.
- **Pause does not freeze data collection.** `space` only freezes the
  display (`frozen_conns` as a snapshot). Collection keeps ticking in
  the background (otherwise resume would cause a rate spike).
  Consequence: on resume, `last_seen` / ghosts are more recent than
  the last-shown view — which is consistent with the "display freezes,
  data doesn't" model.
- **CSV logging doesn't buffer.** One `f.flush()` per tick — robust
  against crashes, but with many clients (hundreds) and a low interval
  (1 s) you'll notice the disk load.
- **The reverse-DNS cache is process-local.** A restart wipes the
  cache. With slow DNS you'll briefly see IPs instead of hostnames
  until the daemon thread catches up.
- **The `Shift+i` toggle affects only the HOST column, not the detail
  popup.** The detail popup always shows the IP plus the hostname (if
  known) — independent of the toggle.

---
