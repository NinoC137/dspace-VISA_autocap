#!/usr/bin/env python3
"""Offline tests for the Tektronix bench backend (tek.py).

Three layers:
  A. Socket framing -- the **real** read_line/read_block code over a
     socket.socketpair() with a scripted server (split chunks, trailing newline,
     non-block responses).
  B. prepare/acquire -- the instrument-coupling guards (timebase vs window,
     record-duration feasibility, sample-rate feasibility).
  C. export -- the HDF5 + manifest contract that analysis.py consumes, including
     the volts -> amperes conversion.

Run: .venv\\Scripts\\python.exe test_tek_backend.py
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tek                                                      # noqa: E402
from common import selected_channels                            # noqa: E402

IDN = "TEKTRONIX,MSO58B,C070122,CF:91.1CT FV:2.14.3.2448"
N_POINTS = 1000
DT = 5.0e-4                      # -> 2000 Sa/s, 0.5 s window at 1000 points
SDIV = 0.05
RAW = [(i % 200) - 100 for i in range(N_POINTS)]     # int16 counts


def base_config():
    channels = {}
    for i in range(1, 9):
        channels["CH%d" % i] = {"enabled": i == 2, "signal": "HV source current",
                                "unit": "A", "zero_offset": 0.0,
                                "zero_calibrated": False,
                                "probe_range_A": 30.0, "probe_V_per_A": 0.2}
    return {
        "schema_version": 1,
        "instrument": {
            "resource": "TCPIP::192.168.2.200::4000::SOCKET",
            "expected_model": "MSO58B",
            "expected_serial": "C070122",
            "query_timeout_ms": 5000,
            "acquisition_timeout_s": 2.5,
            "export_timeout_s": 600,
            "min_throughput_bytes_per_s": 200000,
            "transport": "tek_socket",
        },
        "timing": {"enable_duration_s": 12.0, "settle_s": 6,
                   "record_duration_s": 0.5, "minimum_off_s": 1},
        "acquisition": {"sample_rate_hz": None, "max_samples_per_channel": 100000000,
                        "max_raw_bytes": 4294967296,
                        "anti_alias_verified": False,
                        "probe_bandwidth_verified": False},
        "channels": channels,
        "analysis": {"channel": "CH2", "fsw_hz": 10000.0, "f0_hz": 50.0, "q_max": 5,
                     "welch_segment_s": 0.1, "overlap": 0.5, "dc_span_abs": 0.02,
                     "dc_span_relative": 0.02, "ac_span_relative": 0.05,
                     "make_plots": False},
        "screenshots": {"enabled": True, "spans_s": [0.2], "center_fraction": 0.5,
                        "settle_s": 0.5, "timeout_s": 30},
    }


def ieee_block(payload):
    digits = "%d" % len(payload)
    return b"#" + str(len(digits)).encode() + digits.encode() + payload


# --------------------------------------------------------------------------
# A. real framing over a socketpair
# --------------------------------------------------------------------------
class ScriptedServer(threading.Thread):
    def __init__(self, sock, script):
        super().__init__(daemon=True)
        self.sock = sock
        self.script = list(script)
        self.received = []

    def run(self):
        try:
            for _expect, reply in self.script:
                line = b""
                while not line.endswith(b"\n"):
                    c = self.sock.recv(1)
                    if not c:
                        return
                    line += c
                self.received.append(line.decode().strip())
                if reply is None:
                    self.sock.close()
                    return
                for chunk in reply if isinstance(reply, list) else [reply]:
                    self.sock.sendall(chunk)
        except Exception:                                        # noqa: BLE001
            pass


class TestSocketFraming(unittest.TestCase):
    def _session(self, script, timeout_s=5.0):
        client, server = socket.socketpair()
        srv = ScriptedServer(server, script)
        srv.start()
        session = tek.TekSocketSession("TCPIP::192.168.2.200::4000::SOCKET",
                                       timeout_s=timeout_s)
        client.settimeout(timeout_s)      # injecting bypasses open(), set it here
        session.sock = client
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        return session, srv

    def test_query_line(self):
        s, srv = self._session([("*IDN?", IDN.encode() + b"\n")])
        self.assertEqual(s.query("*IDN?"), IDN)
        srv.join(2)
        self.assertEqual(srv.received, ["*IDN?"])

    def test_block_header_and_split_chunks(self):
        raw = ieee_block(struct.pack(">%dh" % N_POINTS, *RAW)) + b"\n"
        s, _ = self._session([("CURVe?", [raw[:1], raw[1:4], raw[4:11], raw[11:]])])
        payload = s.query_block("CURVe?")
        self.assertEqual(len(payload), N_POINTS * 2)
        self.assertEqual(struct.unpack(">%dh" % N_POINTS, payload), tuple(RAW))

    def test_trailing_newline_consumed(self):
        s, _ = self._session([("CURVe?", ieee_block(b"AB") + b"\n"),
                              ("*IDN?", IDN.encode() + b"\n")])
        self.assertEqual(s.query_block("CURVe?"), b"AB")
        self.assertEqual(s.query("*IDN?"), IDN)      # would be "" if not consumed

    def test_non_block_raises(self):
        s, _ = self._session([("CURVe?", b"NOPE\n")])
        with self.assertRaises(tek.TekError):
            s.query_block("CURVe?")

    def test_peer_close_raises(self):
        s, _ = self._session([("*IDN?", None)])
        with self.assertRaises(tek.TekError):
            s.query("*IDN?")

    def test_timeout_does_not_hang(self):
        s, _ = self._session([("*IDN?", b"")], timeout_s=0.4)
        with self.assertRaises(OSError):
            s.query("*IDN?")

    def test_query_lazily_opens_the_session(self):
        """scope_api 的 raw/state 路径直接 query，从不先 prepare/open。"""
        session = FakeSession()
        backend = tek.Tek(base_config(), session=session)
        self.assertFalse(backend.opened)
        self.assertIn("MSO58B", backend.query("*IDN?"))
        self.assertTrue(backend.opened)

    def test_unopened_session_reports_clearly(self):
        session = tek.TekSocketSession("TCPIP::192.168.2.200::4000::SOCKET")
        with self.assertRaises(tek.TekError) as cm:
            session.query("*IDN?")
        self.assertIn("not open", str(cm.exception))

    def test_query_binary_values_returns_requested_container(self):
        body = struct.pack(">%dh" % N_POINTS, *RAW)
        s, _ = self._session([("CURVe?", ieee_block(body) + b"\n")])
        values = s.query_binary_values("CURVe?", datatype="h", big_endian=True,
                                       container=np.array)
        self.assertEqual(values.size, N_POINTS)
        self.assertEqual(int(values[3]), RAW[3])


class TestResourceParsing(unittest.TestCase):
    def test_parses_host_and_port(self):
        self.assertEqual(tek.parse_socket_resource("TCPIP::192.168.2.200::4000::SOCKET"),
                         ("192.168.2.200", 4000))

    def test_rejects_other_forms(self):
        for bad in ("TCPIP::192.168.2.200::INSTR", "USB0::0x1::INSTR",
                    "TCPIP::192.168.2.200::SOCKET", "nonsense"):
            with self.assertRaises(ValueError):
                tek.parse_socket_resource(bad)

    def test_looks_like_idn(self):
        self.assertTrue(tek.looks_like_idn(IDN))
        self.assertFalse(tek.looks_like_idn(""))
        self.assertFalse(tek.looks_like_idn("\x89PNG\r\n\x1a\n"))
        self.assertFalse(tek.looks_like_idn("a,b,c"))


# --------------------------------------------------------------------------
# B/C. backend behaviour with a scripted session
# --------------------------------------------------------------------------
class FakeSession:
    """Scripted instrument. Mirrors the session surface tek.Tek consumes."""

    def __init__(self, record_length=N_POINTS, sdiv=SDIV, sample_rate=1.0 / DT,
                 timebase_mismatch=False, ignore_scale=False):
        self.record_length = record_length
        self.sdiv = sdiv if not ignore_scale else 0.0
        self.sample_rate = sample_rate
        self.timebase_mismatch = timebase_mismatch
        self.writes = []
        self.queries = []
        self.expected_bytes = 0
        self.timeout = 5.0
        self.connection_info = {"transport": "tek_socket",
                                "resource": "TCPIP::192.168.2.200::4000::SOCKET"}
        self.closed = False
        self.acq_count = 0
        self.acquire_seen = False

    # -- lifecycle --
    def open(self, **kwargs):
        return self

    def terminate(self):
        self.close()

    # -- io --
    def write(self, command):
        self.writes.append(command)
        if command.startswith("HORizontal:SCAle "):
            pass
        if command.startswith("ACQuire:STATE ON"):
            self.acquire_seen = True
            self.acq_count += 1

    def query(self, command, timeout_s=None):
        self.queries.append(command)
        if command == "*IDN?":
            return IDN
        if command == "ALLEv?":
            return '1,"No events to report - new events pending *ESR?"'
        if command == "HORizontal:SCAle?":
            if self.timebase_mismatch:
                return "1.0000E-3"                     # 10 ms window, not 0.5 s
            return "%.4E" % self.sdiv
        if command == "HORizontal:RECOrdlength?":
            return str(self.record_length)
        if command == "HORizontal:SAMPLERate?":
            return "%.4E" % self.sample_rate
        if command == "HORizontal:MODe?":
            return "AUTO"
        if command == "ACQuire:MODe?":
            return "SAMPLE"
        if command == "ACQuire:STATE?":
            return "0"
        if command == "ACQuire:NUMACq?":
            return str(self.acq_count)
        if command == "TRIGger:A:EDGE:SOUrce?":
            return "CH1"
        if command == "TRIGger:A:MODe?":
            return "AUTO"
        if command in ("WFMOutpre:BYT_Nr?",):
            return "2"
        if command == "WFMOutpre:NR_Pt?":
            return str(self.record_length)
        if command == "WFMOutpre:XINCR?":
            return "%.6E" % (1.0 / self.sample_rate)
        if command == "WFMOutpre:XZEro?":
            return "0.0E+0"
        if command == "WFMOutpre:PT_Off?":
            return "0"
        if command == "WFMOutpre:YMUlt?":
            return "1.0E-3"                            # 1 mV per count
        if command == "WFMOutpre:YOFf?":
            return "0"
        if command == "WFMOutpre:YZEro?":
            return "0"
        if command == "WFMOutpre:YUNit?":
            return '"V"'
        if command == "WFMOutpre:XUNit?":
            return '"s"'
        if command == "WFMOutpre?":
            return '2;16;BINARY;RI;INTEGER;MSB;"Ch2";%d;Y;LINEAR;"s"' % self.record_length
        if command == "DATa:ENCdg?":
            return "RIBINARY"
        if command.endswith(":COUPling?"):
            return "DC"
        if command.endswith(":SCAle?"):
            return "100.0000E-3"
        if command.endswith(":OFFSet?"):
            return "0.0E+0"
        if command.endswith(":TERmination?"):
            return "1.0000E+6"
        if command.endswith(":BANdwidth?"):
            return "500.0000E+6"
        if command.endswith(":PROBe:GAIN?"):
            return "1.0000"
        raise tek.TekError("unsupported query in fake: %s" % command)

    def query_binary_values(self, command, datatype="h", big_endian=True,
                            container=list, timeout_s=None):
        self.queries.append(command)          # record only; CURVe? has no line response
        body = struct.pack(">%dh" % self.record_length, *RAW[:self.record_length])
        values = struct.unpack(">%dh" % self.record_length, body)
        return container(values)

    def check_storage(self, bytes_required, directory=None):
        return True

    def close(self):
        self.closed = True


def make_backend(session=None, **cfg_overrides):
    config = base_config()
    for key, value in cfg_overrides.items():
        config["timing"][key] = value
    session = session or FakeSession()
    return tek.Tek(config, session=session), session, config


class TestPrepare(unittest.TestCase):
    def test_configures_timebase_and_records_actuals(self):
        backend, session, _ = make_backend()
        with tempfile.TemporaryDirectory() as tmp:
            settings = backend.prepare(tmp)
            self.assertTrue(Path(tmp, "settings.json").exists())
        self.assertIn("HORizontal:MODe AUTO", session.writes)
        self.assertIn("HORizontal:SCAle 0.05", session.writes)
        acq = settings["acquisition"]
        self.assertEqual(acq["record_length"], N_POINTS)
        self.assertAlmostEqual(acq["resolution_s"], DT, places=12)
        self.assertAlmostEqual(acq["duration_s"], 0.5, places=9)
        self.assertEqual(acq["requested_duration_s"], 0.5)
        self.assertEqual(settings["identity"]["model"], "MSO58B")

    def test_rejects_model_mismatch(self):
        backend, session, config = make_backend()
        config["instrument"]["expected_model"] = "MSO64"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                backend.prepare(tmp)
        self.assertIn("Model mismatch", str(cm.exception))

    def test_rejects_serial_mismatch(self):
        backend, session, config = make_backend()
        config["instrument"]["expected_serial"] = "WRONG"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                backend.prepare(tmp)
        self.assertIn("Serial mismatch", str(cm.exception))

    def test_rejects_timebase_window_disagreement(self):
        """记录窗口和时基对不上必须报错，否则会悄悄采到错的时长。"""
        backend, _, _ = make_backend(session=FakeSession(timebase_mismatch=True))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                backend.prepare(tmp)
        self.assertIn("disagree", str(cm.exception))

    def test_rejects_impossible_duration(self):
        """真机上把 0.5 s 请求成 0.01 s 就是这种情况，必须挡住。"""
        # self-consistent timebase (1000 pts @ 1 MSa/s = 1 ms = 10 x 0.1 ms/div)
        # but nowhere near the requested 0.5 s window.
        backend, _, _ = make_backend(session=FakeSession(record_length=1000,
                                                        sample_rate=1.0e6,
                                                        sdiv=1.0e-4))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                backend.prepare(tmp)
        self.assertIn("did not accept the requested record duration", str(cm.exception))

    def test_rejects_unhonourable_sample_rate(self):
        backend, _, config = make_backend()
        config["acquisition"]["sample_rate_hz"] = 1.0e6      # fake reports 2000
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as cm:
                backend.prepare(tmp)
        self.assertIn("sample_rate_hz", str(cm.exception))
        self.assertIn("record_duration_s", str(cm.exception))


class TestAcquireAndExport(unittest.TestCase):
    def _prepared(self, tmp):
        backend, session, config = make_backend()
        backend.prepare(tmp)
        backend.acquire()
        return backend, session, config

    def test_public_identity_contract(self):
        """scope_api.read_state calls scope.identity() and expects the raw string."""
        backend, _, _ = make_backend()
        self.assertEqual(backend.identity(), IDN)

    def test_identity_rejects_wrong_model(self):
        backend, _, config = make_backend()
        config["instrument"]["expected_model"] = "MSO64"
        with self.assertRaises(ValueError):
            backend.identity()

    def test_acquire_sets_stamp_and_errors_are_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, session, _ = self._prepared(tmp)
        self.assertTrue(backend.acquired)
        self.assertIsNotNone(backend.acquisition_stamp)
        self.assertIn("ACQuire:STATE ON", session.writes)

    def test_export_writes_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, session, config = self._prepared(tmp)
            raw = backend.export(Path(tmp), {"case_id": "unit"})
            self.assertEqual(raw.name, "waveforms.h5")
            self.assertTrue(raw.exists())
            self.assertFalse(Path(tmp, "waveforms.h5.partial").exists())

            import h5py
            with h5py.File(raw, "r") as handle:
                self.assertEqual(handle.attrs["backend"], "tek")
                self.assertEqual(json.loads(handle.attrs["config_json"])["schema_version"], 1)
                group = handle["channels/CH2"]
                self.assertEqual(group["samples"].shape, (N_POINTS,))
                self.assertEqual(group["samples"].dtype, np.dtype("<f4"))
                self.assertEqual(group.attrs["unit"], "A")
                self.assertEqual(group.attrs["instrument_yunit"], "V")
                self.assertAlmostEqual(float(group.attrs["volts_per_unit"]), 0.2)
                self.assertAlmostEqual(float(group.attrs["dt_s"]), DT, places=12)
                self.assertEqual(int(group.attrs["N"]), N_POINTS)
                # volts = raw * 1 mV ; amps = volts / 0.2
                samples = group["samples"][:]
                expected = np.array(RAW, dtype=np.float64) * 1.0e-3 / 0.2
                self.assertTrue(np.allclose(samples, expected, atol=1e-9))

            manifest = json.loads(Path(tmp, "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["capture_status"], "complete")
            self.assertEqual(manifest["backend"], "tek")
            self.assertEqual(manifest["channels"], ["CH2"])
            self.assertEqual(manifest["file"], "waveforms.h5")
            self.assertEqual(len(manifest["sha256"]), 64)
            self.assertEqual(manifest["connection"]["transport"], "tek_socket")

    def test_export_sets_full_record_span(self):
        """DATa:STOP 默认 1000000 会截断记录，导出必须显式设整条。"""
        with tempfile.TemporaryDirectory() as tmp:
            backend, session, _ = self._prepared(tmp)
            backend.export(Path(tmp), {})
        self.assertIn("DATa:STARt 1", session.writes)
        self.assertIn("DATa:STOP %d" % N_POINTS, session.writes)
        self.assertIn("DATa:SOUrce CH2", session.writes)

    def test_export_refuses_without_acquisition(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, _, _ = make_backend()
            backend.prepare(tmp)
            with self.assertRaises(RuntimeError):
                backend.export(Path(tmp), {})

    def test_export_detects_acquisition_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, session, _ = self._prepared(tmp)
            session.acq_count += 5                     # someone re-armed the scope
            with self.assertRaises(RuntimeError):
                backend.export(Path(tmp), {})

    def test_close_stops_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend, session, _ = self._prepared(tmp)
            backend.close(stop=True)
        self.assertIn("ACQuire:STATE OFF", session.writes)
        self.assertTrue(session.closed)


class TestIntegrationWithCommon(unittest.TestCase):
    def test_config_passes_own_validator(self):
        """New transport must survive the bench's own load/validate path."""
        import common
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "bench_config.json")
            path.write_text(json.dumps(base_config()), encoding="utf-8")
            loaded = common.load_config(str(path))
            self.assertEqual(loaded["instrument"]["transport"], "tek_socket")
            self.assertEqual(selected_channels(loaded), ["CH2"])

    def test_bad_resource_is_rejected_by_validator(self):
        import common
        config = base_config()
        config["instrument"]["resource"] = "TCPIP::192.168.2.200::INSTR"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "bench_config.json")
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ValueError):
                common.load_config(str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
