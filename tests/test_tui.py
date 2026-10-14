"""End-to-end keyboard test: drives the real TUI in a 12x100 pseudo-terminal.

50 fake clients, default sort SENT descending, so visible[i] is 10.0.0.(50-i)
and 12 rows leave 8 data rows (title, spacer, header, footer). Runs ~12 s
because it also waits for the 5 s cursor auto-hide. Mouse input is fed as
SGR sequences (ESC [ < b ; x ; y M), which ncurses decodes once
mousemask() is active. A second scenario replays connect/disconnect/alert transitions
and checks the watchdog log.
"""
import fcntl
import os
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
UP, DOWN, PGUP, PGDN, HOME, END = "\x1bOA", "\x1bOB", "\x1b[5~", "\x1b[6~", "\x1bOH", "\x1bOF"
THUMB, TRACK = "█".encode(), "│".encode()


def mouse(button, x, y, release=False):
    # SGR (1006) mouse report, the encoding ncurses decodes in the test pty:
    # ESC [ < button ; col ; row M   (m = release). 64 = wheel up,
    # 65 = wheel down, 0 = button 1; coordinates are 1-based.
    return f"\x1b[<{button};{x + 1};{y + 1}{'m' if release else 'M'}"


class PtyMixin:
    def start(self, *harness_args):
        self.tmp = tempfile.mkdtemp(prefix="nfsmon-tui-")
        self.log = os.path.join(self.tmp, "frames.log")
        self.err = os.path.join(self.tmp, "child.err")
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 100, 0, 0))
        # ESCDELAY: ncurses waits that long (ms, default 1000) after a lone ESC
        env = dict(os.environ, TERM="xterm", HOME=self.tmp, LANG="C.UTF-8", ESCDELAY="50")
        self.errf = open(self.err, "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "tui_harness.py"), self.log, *harness_args],
            stdin=slave, stdout=slave, stderr=self.errf, env=env)
        os.close(slave)
        self.raw = bytearray()
        threading.Thread(target=self._drain, daemon=True).start()
        time.sleep(1.5)

    def tearDown(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.errf.close()
        os.close(self.master)

    def _drain(self):
        while True:
            try:
                self.raw.extend(os.read(self.master, 65536))
            except OSError:
                return

    def last(self):
        with open(self.log) as f:
            return f.read().strip().splitlines()[-1]

    def send(self, keys, wait=0.35):
        os.write(self.master, keys.encode())
        time.sleep(wait)
        return self.last()

    def expect(self, label, got, *want):
        for w in want:
            self.assertIn(w, got, f"{label}: {got}")

    def quit_and_check(self):
        self.send("q", wait=0.5)
        rc = self.proc.wait(timeout=5)
        with open(self.err) as f:
            err = f.read()
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "", err)


class TuiTest(PtyMixin, unittest.TestCase):
    def setUp(self):
        self.start("scroll")

    def test_scrolling_paging_jumps_and_grouping(self):
        self.expect("start", self.last(), "scroll=0", "sel=None", "n=50", "rows=12")
        self.assertTrue(THUMB in self.raw and TRACK in self.raw, "scrollbar at start")
        self.expect("down w/o cursor", self.send(DOWN), "scroll=0", "sel=10.0.0.50")
        self.expect("G", self.send("G"), "scroll=42", "sel=10.0.0.1")
        self.expect("gg", self.send("gg"), "scroll=0", "sel=10.0.0.50", "group=False")
        self.expect("End", self.send(END), "scroll=42", "sel=10.0.0.1")
        self.expect("Home", self.send(HOME), "scroll=0", "sel=10.0.0.50")
        self.expect("PgDn", self.send(PGDN), "scroll=3", "sel=10.0.0.40")
        self.expect("PgDn 2", self.send(PGDN), "scroll=13", "sel=10.0.0.30")
        self.expect("Down", self.send(DOWN), "scroll=14", "sel=10.0.0.29")
        self.expect("wheel up", self.send(mouse(64, 10, 5)), "scroll=14", "sel=10.0.0.32")
        self.expect("wheel down", self.send(mouse(65, 10, 5)), "scroll=14", "sel=10.0.0.29")
        # click on the 3rd data row (screen row FIRST_DATA_ROW + 2) selects visible[scroll + 2]
        self.expect("click", self.send(mouse(0, 10, 3 + 2) + mouse(0, 10, 3 + 2, release=True)),
                    "scroll=14", "sel=10.0.0.34")
        self.expect("cols right", self.send("\x1bOC"), "col=1")
        self.expect("cols left", self.send("\x1bOD"), "col=0")
        self.expect("help open", self.send("h"), "help=0")
        self.expect("help scrolls", self.send(DOWN * 3), "help=3", "sel=10.0.0.34")
        self.expect("help wheel", self.send(mouse(65, 10, 5)), "help=6")
        self.expect("help close", self.send("h"), "help=-", "sel=10.0.0.34")
        # detail popup: 60 lines x 200 cols inside a 12x100 pty → page 8, box width 98
        self.expect("detail open", self.send("\r"), "detail=0/0", "sel=10.0.0.34")
        self.expect("detail down", self.send(DOWN), "detail=1/0")
        self.expect("detail pgdn", self.send(PGDN), "detail=9/0")
        self.expect("detail right", self.send("\x1bOC" * 2), "detail=9/16")
        self.expect("detail wheel", self.send(mouse(65, 10, 5)), "detail=12/16")
        self.expect("detail end", self.send(END), "detail=52/16")
        self.expect("detail home", self.send(HOME), "detail=0/0")
        self.expect("detail close", self.send("\x1b"), "detail=0/-", "sel=10.0.0.34")
        self.expect("back to idx 21", self.send(DOWN * 5), "scroll=14", "sel=10.0.0.29")
        self.expect("PgUp", self.send(PGUP), "scroll=11", "sel=10.0.0.39")
        self.expect("Up x12 clamps", self.send(UP * 12), "scroll=0", "sel=10.0.0.50")
        mark = len(self.raw)
        self.expect("lone g groups", self.send("g", wait=0.9), "group=True", "n=1")
        time.sleep(0.5)
        self.assertNotIn(THUMB, self.raw[mark:], "no scrollbar when the list fits")
        self.expect("g again", self.send("g", wait=0.9), "group=False", "n=50")
        self.expect("g then s settles g", self.send("gs", wait=0.4), "group=True")
        self.expect("g back", self.send("g", wait=0.9), "group=False")
        mark = len(self.raw)
        self.expect("End again", self.send(END), "scroll=42", "sel=10.0.0.1")
        time.sleep(0.4)
        self.assertIn(THUMB, self.raw[mark:], "scrollbar redrawn after End")
        time.sleep(5.5)
        self.expect("auto-hide keeps scroll", self.last(), "scroll=42", "sel=None")
        self.expect("up w/o cursor", self.send(UP), "scroll=42", "sel=10.0.0.1")
        self.expect("follow", self.send("f"), "sel=10.0.0.1")
        self.expect("Home tracks pin", self.send(HOME), "scroll=0", "sel=10.0.0.50")
        self.send("f", wait=0.3)
        self.quit_and_check()


class WatchdogTest(PtyMixin, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="nfsmon-wd-")
        self.events = os.path.join(tmp, "events.log")
        self.start("watchdog", os.path.join(tmp, "nfsmon.conf"), self.events)

    def test_transitions_are_logged_in_order(self):
        time.sleep(7.5)                       # ticks 1 s apart; tick 6 clears the alert
        self.quit_and_check()
        with open(self.events) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "timestamp,event,ip,host,last_sent,last_recv")
        events = [tuple(l.split(",")[1:4]) for l in lines[1:]]
        self.assertEqual(events, [
            ("disconnect", "10.0.0.3", "c"),      # tick 3
            ("connect",    "10.0.0.4", "d"),      # tick 4 (never seen before)
            ("alert",      "10.0.0.1", "a"),      # tick 4 (20 MB/s for >= 2 s)
            ("reconnect",  "10.0.0.3", "c"),      # tick 5 (back within 300 s)
            ("alert_clear", "10.0.0.1", "a"),     # tick 6 (burst over)
        ])


if __name__ == "__main__":
    unittest.main()
