"""Transport regressions using real child processes/files, no network or hardware."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import h5py
import numpy as np

from common import atomic_json, load_config
from mxo import Mxo
from pi_agent import check_mount, stream_block
from pi_transport import PiSession, verify_delivery

HERE = Path(__file__).parent


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = load_config(HERE/"bench_config.json")
        # These tests drive the Raspberry-Pi transport with a fake peer, so pin the
        # instrument section rather than depending on which scope is currently
        # deployed by switch-scope.cmd.
        self.cfg["instrument"].update({
            "resource": "USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR",
            "expected_model": "MXO38",
            "expected_serial": "1335.2050k08/101400",
            "transport": "raspi_ssh"})
        self.cfg["instrument"]["remote"]["local_share_root"] = str(self.root)
        self.cfg["instrument"]["remote"]["host"] = "fixture@localhost"
        self.cfg["instrument"]["query_attempts"] = 1
        for name, item in self.cfg["channels"].items():
            item["enabled"] = name in ("CH1", "CH8")
        atomic_json(self.root/"config.json", self.cfg)

    def session(self):
        popen = subprocess.Popen
        def start(_args, **kwargs):
            return popen([sys.executable, "-u", str(HERE/"test_pi_peer.py"),
                          str(self.root), str(self.root/"config.json")], **kwargs)
        with patch("pi_transport.subprocess.Popen", side_effect=start):
            session = PiSession(self.cfg)
        self.addCleanup(session.close)
        return session

    def test_full_multichannel_export_via_stdio_and_share(self):
        session = self.session()
        scope = Mxo(self.cfg, session=session)
        scope.prepare(self.root)
        scope.acquire()
        raw = scope.export(self.root, {})
        scope.close()
        with h5py.File(raw) as h:
            self.assertEqual(set(h["channels"]), {"CH1", "CH8"})
            self.assertEqual(h["channels/CH1"].attrs["unit"], "A")
            self.assertEqual(h["channels/CH8"].attrs["unit"], "V")
            self.assertEqual(h["channels/CH1"].attrs["dt_s"], 1e-5)
            y = h["channels/CH1/samples"][:]
            self.assertAlmostEqual(float(np.mean(y.astype(float)**2)), 11.5, places=5)
        self.assertFalse(list(self.root.rglob("*.f32")))
        manifest = json.loads((self.root/"manifest.json").read_text())
        self.assertTrue(manifest["connection"]["storage_verified"])

    def test_disconnect_and_timeout_invalidate_session(self):
        session = self.session()
        with self.assertRaises(ConnectionError):
            session.query("TEST:DISCONNECT?")
        session.close()
        session = self.session()
        with self.assertRaises(TimeoutError):
            session.rpc("query", command="TEST:TIMEOUT?", wait_s=0.1)
        with self.assertRaises(ConnectionError):
            session.query("*IDN?")

    def test_mount_guard_rejects_local_directory(self):
        with patch("pi_agent.Path.read_text", return_value="/dev/mmc / ext4 rw 0 0\n"):
            with self.assertRaisesRegex(RuntimeError, "SMB mount missing"):
                check_mount(self.root)

    def test_wrong_mapping_or_corrupt_file_is_not_accepted(self):
        path = self.root/"wrong.f32"
        path.write_bytes(b"1234")
        with self.assertRaisesRegex(IOError, "checksum mismatch"):
            verify_delivery(path, dict(bytes=4, sha256="0"*64), timeout_s=0)

    def test_block_larger_than_old_64mb_limit_streams_in_chunks(self):
        n = 65*1024**2
        prefix = io.BytesIO(b"#8"+str(n).encode())
        remaining = n
        reads = []
        def read(count):
            nonlocal remaining
            reads.append(count)
            if prefix.tell() < len(prefix.getvalue()):
                return prefix.read(count)
            if remaining:
                size = min(count, remaining)
                remaining -= size
                return bytes(size)
            return b"\n"
        result = stream_block(read, self.root/"large.f32", n)
        self.assertEqual(result["bytes"], n)
        self.assertLessEqual(max(reads), 65536)

    def test_bad_blocks_never_produce_final_file(self):
        for index, payload in enumerate((b"#18abcdefgh\n", b"#28xxxx", b"#0", b"#216short")):
            target = self.root/f"bad{index}.f32"
            with self.assertRaises((ValueError, EOFError)):
                stream_block(io.BytesIO(payload).read, target, 16)
            self.assertFalse(target.exists())

    def test_png_stdio_smb_transfer_is_byte_exact_and_cleans_spool(self):
        from test_screenshots import png_bytes
        session = self.session()
        self.assertEqual(session.screenshot_bytes(), png_bytes())
        self.assertFalse(list(self.root.rglob("*.png")))

    def test_unknown_length_block_rejects_oversize_before_payload(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            stream_block(io.BytesIO(b"#833554433").read, self.root/"oversize.png", None)
        self.assertFalse((self.root/"oversize.png").exists())


if __name__ == "__main__":
    unittest.main()
