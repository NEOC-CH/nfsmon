"""End-to-end keyboard test: drives the real TUI in a 12x100 pseudo-terminal.

50 fake clients, default sort SENT descending, so visible[i] is 10.0.0.(50-i)
and 12 rows leave 8 data rows (title, spacer, header, footer). Runs ~12 s
because it also waits for the 5 s cursor auto-hide.
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


class TuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nfsmon-tui-")
        self.log = os.path.join(self.tmp, "frames.log")
        self.err = os.path.join(self.tmp, "child.err")
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 100, 0, 0))
        env = dict(os.environ, TERM="xterm", HOME=self.tmp, LANG="C.UTF-8")
        self.errf = open(self.err, "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "tui_harness.py"), self.log],
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
        self.send("q", wait=0.5)
        rc = self.proc.wait(timeout=5)
        with open(self.err) as f:
            err = f.read()
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, "", err)


if __name__ == "__main__":
    unittest.main()
