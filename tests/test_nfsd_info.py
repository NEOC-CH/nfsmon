"""read_nfsd_clients_info() against a fake /proc tree.

The RHEL 9 `states` sample is the real format captured from a 5.14 kernel:
filenames carry only the last two path components, so MOUNT has to be
derived from the superblock device and the export table.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nfsmon  # noqa: E402


class NfsdInfoTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nfsmon-test-")
        st = os.stat(self.root)
        self.dev = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}"
        self.clients = os.path.join(self.root, "clients")
        self._saved = (nfsmon.NFSD_CLIENTS_DIR, nfsmon.EXPORT_TABLE_PATHS)
        nfsmon.NFSD_CLIENTS_DIR = self.clients
        nfsmon._health.clear()

    def tearDown(self):
        nfsmon.NFSD_CLIENTS_DIR, nfsmon.EXPORT_TABLE_PATHS = self._saved
        nfsmon._health.clear()
        shutil.rmtree(self.root)

    # -- helpers ------------------------------------------------------------
    def client(self, cid, ip, states):
        d = os.path.join(self.clients, cid)
        os.makedirs(d)
        with open(os.path.join(d, "info"), "w") as f:
            f.write(f'clientid: 0x1\naddress: "{ip}:739"\nstatus: confirmed\n'
                    f'name: "Linux NFSv4.2 x"\nminor version: 2\n')
        with open(os.path.join(d, "states"), "w") as f:
            f.write(states)

    def exports(self, text):
        p = os.path.join(self.root, "exports")
        with open(p, "w") as f:
            f.write(text)
        nfsmon.EXPORT_TABLE_PATHS = (os.path.join(self.root, "missing"), p)

    def rhel_states(self, dev=None):
        dev = dev or self.dev
        return (f'- 0x1: {{ type: open, access: r-, deny: --, superblock: "{dev}:9470874377", '
                f'filename: "dconf/.nfs000000023482130900000079", owner: "open id:\\x00" }}\n'
                f'- 0x2: {{ type: open, access: rw, deny: --, superblock: "{dev}:832205456", '
                f'filename: "/", owner: "open id:\\x00" }}\n'
                f'- 0x3: {{ type: deleg, access: w, superblock: "{dev}:3825630253", '
                f'filename: ".coffer/turo.coffer" }}\n')

    # -- tests --------------------------------------------------------------
    def test_pseudo_root_gives_way_to_sub_export(self):
        exp = os.path.join(self.root, "opt", "exports")
        os.makedirs(os.path.join(exp, "nfs-home"))
        self.exports(f"# Version 1.1\n{exp}\t10.0.0.0/20(ro,fsid=0)\n{exp}/nfs-home\t10.0.0.0/20(rw)\n")
        self.client("1", "10.0.10.113", self.rhel_states())
        info = nfsmon.read_nfsd_clients_info()
        self.assertEqual(info["10.0.10.113"],
                         {"nfsv": "4.2", "mount": f"{exp}/nfs-home", "states": (2, 0, 1)})

    def test_octal_escaped_export_path(self):
        os.makedirs(os.path.join(self.root, "exp two"))
        self.exports(f"{self.root}/exp\\040two 10.0.0.0/20(rw)\n")
        self.client("1", "10.0.10.113", self.rhel_states())
        self.assertEqual(nfsmon.read_nfsd_clients_info()["10.0.10.113"]["mount"],
                         f"{self.root}/exp two")

    def test_sibling_exports_collapse_to_common_prefix(self):
        os.makedirs(os.path.join(self.root, "sib", "a"))
        os.makedirs(os.path.join(self.root, "sib", "b"))
        self.exports(f"{self.root}/sib/a *(rw)\n{self.root}/sib/b *(rw)\n")
        self.client("1", "10.0.10.113", self.rhel_states())
        self.assertEqual(nfsmon.read_nfsd_clients_info()["10.0.10.113"]["mount"],
                         f"{self.root}/sib")

    def test_unknown_device_or_no_states_is_empty(self):
        self.exports("/nonexistent *(rw)\n")
        self.client("1", "10.0.10.113", self.rhel_states("ab:cd"))
        self.client("2", "10.0.10.167", "")
        info = nfsmon.read_nfsd_clients_info()
        self.assertEqual(info["10.0.10.113"]["mount"], "")
        self.assertEqual(info["10.0.10.167"]["mount"], "")

    def test_absolute_filenames_use_common_prefix(self):
        self.exports("")
        self.client("1", "10.0.10.222",
                    '- 0x1: { type: open, superblock: "fd:00:1", filename: "/export/home/turo/.bashrc", owner: "o" }\n'
                    '- 0x2: { type: open, superblock: "fd:00:2", filename: "/export/home/turo/work/a.txt", owner: "o" }\n'
                    '- 0x3: { type: open, superblock: "fd:00:3", filename: "/", owner: "o" }\n')
        self.assertEqual(nfsmon.read_nfsd_clients_info()["10.0.10.222"]["mount"],
                         "/export/home/turo")

    def test_need_states_false_skips_states(self):
        self.exports("")
        self.client("1", "10.0.10.113", "THIS IS NOT PARSEABLE AND MUST NOT BE READ")
        info = nfsmon.read_nfsd_clients_info(need_states=False)
        self.assertEqual(info["10.0.10.113"], {"nfsv": "4.2", "mount": "", "states": None})

    def test_health_warning_when_dir_missing_and_cleared_after(self):
        nfsmon.read_nfsd_clients_info()
        self.assertTrue(any("nfsd" in w for w in nfsmon.health_warnings()))
        self.exports("")
        self.client("1", "10.0.10.113", "")
        nfsmon.read_nfsd_clients_info()
        self.assertEqual(nfsmon.health_warnings(), [])


if __name__ == "__main__":
    unittest.main()
