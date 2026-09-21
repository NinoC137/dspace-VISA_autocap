import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

import h5py
import numpy as np
from pyvisa.errors import VisaIOError

from analysis import basic_metrics, current_metrics, analyze_file, summarize
from bench import worker
from common import atomic_json, load_config, read_json, selected_channels, validate
from mxo import Mxo, parse_header

HERE = Path(__file__).parent


class FakeVisa:
    def __init__(self, config, fault=""):
        self.config, self.fault = config, fault
        self.states = {i: False for i in range(1, 9)}
        self.writes, self.reads = [], []
        self.closed = False
        self.fs, self.n = 100000, 50000
        self.clears = 0
        # A pending response is what makes every later operation fail; it is released
        # only when the host aborts the transfer with a VISA clear.
        self.pending_stale = False
        self.dead = False
        self.fail_next = 0

    def clear(self):
        self.clears += 1
        # A definite clear releases a pending response, but a dead instrument stays
        # dead: that is the case the operator must power-cycle.
        if not self.dead:
            self.pending_stale = False

    def _raise_if_stale(self):
        if self.dead or self.pending_stale:
            raise VisaIOError(-1073807339)
        if self.fail_next:
            self.fail_next -= 1
            self.pending_stale = True
            raise VisaIOError(-1073807339)

    def write(self, command):
        if command.upper().startswith("ACQUIRE:SRREAL "):
            raise RuntimeError('SCPI -171: instrument rejects ACQuire:SRReal writes')
        self.writes.append(command)
        if command.startswith("CHANnel") and ":STATe " in command:
            number = int(command.split(":")[0][7:])
            self.states[number] = command.endswith(" 1")

    def query(self, command):
        self.reads.append(command)
        self._raise_if_stale()
        values = {"*IDN?": "Rohde&Schwarz,MXO38,1335.2050k08/101400,2.8.2.0", "*OPC?": "1",
                  "SYSTem:ERRor?": '0,"No error"', "ACQuire:RESolution?": "0.00001",
                  "ACQuire:SRReal?": "100000", "ACQuire:RLReal?": "50000", "ACQuire:TYPE?": "SAMP",
                  "ACQuire:COUNt?": "1", "TIMebase:RANGe?": "0.5", "TRIGger:MODE?": "AUTO",
                  "ACQuire:POMemory?": "0", "ACQuire:HISTory:ISODate?": '"2026-09-20T10:00:00.123456789"'}
        if self.fault == "timeout" and "RUNSingle" in self.writes and command == "*OPC?":
            raise TimeoutError("injected acquisition timeout")
        if command in values:
            return values[command]
        if command.startswith("PROBe"):
            return "A" if command.startswith("PROBe1:") else "V"
        if command.startswith("CHANnel"):
            number = int(command.split(":")[0][7:])
            tail = command.split(":", 1)[1]
            return {"STATe?": str(int(self.states[number])), "SCALe?": "1", "OFFSet?": "0",
                    "COUPling?": "DC", "BANDwidth?": "20000", "WAVeform:TYPE?": "SAMP",
                    "WAVeform:ARIThmetics?": "OFF", "OVERload:STATus:CONDition?": "0",
                    "DATA:HEADer?": "0,0.49999,50000,1"}[tail]
        raise AssertionError(command)

    def query_binary_values(self, command, **kwargs):
        self.reads.append(command)
        self._raise_if_stale()
        t = np.arange(self.n) / self.fs
        y = 3 + 2*np.sin(2*np.pi*10000*t) + np.sin(2*np.pi*20000*t)
        if self.fault == "short":
            return y[:-1]
        if self.fault == "nonfinite":
            y[50] = np.nan
        return y

    def close(self):
        self.closed = True


class BenchTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(HERE / "bench_config.json")
        self.cfg["screenshots"]["enabled"] = False
        # Tests use CH1 regardless of the operator's current channel selection.
        for name, channel in self.cfg["channels"].items():
            channel["enabled"] = name == "CH1"
        self.cfg["analysis"]["make_plots"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_channel_selection(self):
        self.assertEqual(selected_channels(self.cfg), ["CH1"])
        self.cfg["channels"]["CH1"]["enabled"] = False
        with self.assertRaises(ValueError):
            validate(self.cfg)
        self.cfg["channels"]["CH8"]["enabled"] = True
        validate(self.cfg)
        self.assertEqual(selected_channels(self.cfg), ["CH8"])
        self.cfg["channels"]["CH8"]["enabled"] = "false"
        with self.assertRaises(ValueError):
            validate(self.cfg)

    def test_rms_and_carrier_energy(self):
        fs = 100000
        t = np.arange(50000)/fs
        y = 3 + 2*np.sin(2*np.pi*10000*t) + np.sin(2*np.pi*20000*t)
        m, _ = current_metrics(y, fs, self.cfg)
        self.assertAlmostEqual(m["dc"], 3, places=10)
        self.assertAlmostEqual(m["rms"]**2, 11.5, places=10)
        self.assertAlmostEqual(m["rms_ac"]**2, 2.5, places=10)
        self.assertAlmostEqual(m["two_group_mean_square_A2"], 2.5, places=9)
        self.assertAlmostEqual(m["two_group_A2s"], 1.25, places=9)

    def test_boundary_sidebands(self):
        fs = 100000
        t = np.arange(50000)/fs
        y = np.cos(2*np.pi*10250*t) + np.cos(2*np.pi*20250*t)
        m, _ = current_metrics(y, fs, self.cfg)
        self.assertAlmostEqual(m["groups"]["1"]["group"]["mean_square_A2"], 0.5, places=8)
        self.assertAlmostEqual(m["groups"]["1"]["strict"]["mean_square_A2"], 0.25, places=8)

    def test_parseval_odd_even_noncoherent(self):
        rng = np.random.default_rng(2)
        for n in (50000, 50001):
            t = np.arange(n)/100000
            y = 6 + rng.normal(size=n) + np.cos(2*np.pi*10003.2*t)
            m, _ = current_metrics(y, 100000, self.cfg)
            self.assertAlmostEqual(m["parseval_spectral_A2"], m["parseval_windowed_time_A2"], places=10)
        with self.assertRaises(ValueError):
            current_metrics(np.ones(50000), 10000, self.cfg)
        with self.assertRaises(ValueError):
            basic_metrics([1, float("nan")])

    def test_header_rejects_envelopes_and_wrong_timebase(self):
        self.assertEqual(parse_header("0,0.49999,50000,1", 1e-5)["N"], 50000)
        parse_header("0,0.5,50000,1", 1e-5)
        for text in ("0,0.5,50000,2", "0,0.5,500.5,1", "0,2,50000,1", "0,nan,2,1"):
            with self.assertRaises(ValueError):
                parse_header(text, 1e-5)

    def test_readonly_snapshot(self):
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        scope.snapshot()
        scope.close()
        self.assertEqual(visa.writes, [])

    def test_prepare_sample_rate_control(self):
        for requested in (None, 100000):
            with self.subTest(requested=requested):
                self.cfg["acquisition"]["sample_rate_hz"] = requested
                visa = FakeVisa(self.cfg)
                scope = Mxo(self.cfg, visa)
                scope.prepare(self.path)
                self.assertIn("ACQuire:SRATe:VALue 100000", visa.writes)
                self.assertLess(visa.writes.index("ACQuire:SRATe:MODE MANual"),
                                visa.writes.index("ACQuire:SRATe:VALue 100000"))
                self.assertIn("ACQuire:SRReal?", visa.reads)
                scope.close()

    def test_prepare_rejects_sample_rate_readback_mismatch(self):
        self.cfg["acquisition"]["sample_rate_hz"] = 200000
        scope = Mxo(self.cfg, FakeVisa(self.cfg))
        with self.assertRaisesRegex(ValueError, "could not maintain"):
            scope.prepare(self.path)
        self.assertFalse((self.path / "settings.json").exists())
        scope.close()

    def export_fixture(self, config=None, fault=""):
        config = config or self.cfg
        visa = FakeVisa(config, fault)
        scope = Mxo(config, visa)
        scope.prepare(self.path)
        scope.acquire()
        path = scope.export(self.path, {"Case": {"Id": "test", "VL": 120}, "Readback": {"Xi": 0.5}})
        scope.close()
        return path, visa

    def test_only_selected_channels_saved_and_no_double_scaling(self):
        self.cfg["channels"]["CH8"]["enabled"] = True
        raw, visa = self.export_fixture()
        with h5py.File(raw) as h:
            self.assertEqual(set(h["channels"]), {"CH1", "CH8"})
            self.assertAlmostEqual(float(h["channels/CH1/samples"][:].mean()), 3, places=6)
        self.assertNotIn("CHANnel2:DATA:VALues?", visa.reads)
        self.assertIn("CHANnel2:STATe 0", visa.writes)
        report_path = analyze_file(raw)
        m = read_json(self.path / "manifest.json")
        m["metrics"] = str(report_path.relative_to(self.path))
        atomic_json(self.path / "manifest.json", m)
        self.assertEqual(summarize(self.path), 1)
        self.assertEqual(summarize(self.path), 1)

    def test_ch1_disabled_skips_current_analysis(self):
        self.cfg["channels"]["CH1"]["enabled"] = False
        self.cfg["channels"]["CH2"]["enabled"] = False
        self.cfg["channels"]["CH3"]["enabled"] = True
        self.cfg["channels"]["CH3"]["unit"] = "V"
        raw, _ = self.export_fixture()
        report = read_json(analyze_file(raw))
        self.assertIn("skipped", report["current_analysis"])
        self.assertEqual(set(report["channels"]), {"CH3"})

    def test_incomplete_transfer_does_not_commit(self):
        for fault in ("short", "nonfinite"):
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.export_fixture(fault=fault)
            self.assertFalse((self.path / "waveforms.h5").exists())
            self.assertFalse((self.path / "manifest.json").exists())

    def test_export_accepts_one_point_acquisition_header_difference(self):
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        scope.prepare(self.path)
        scope.settings["acquisition"]["record_length"] = "50001"
        scope.acquire()
        raw = scope.export(self.path, {})
        with h5py.File(raw) as h:
            g = h["channels/CH1"]
            self.assertEqual(g.attrs["N"], 50000)
            self.assertEqual(g["samples"].shape, (50000,))
        scope.close()

    def test_export_rejects_larger_acquisition_header_difference(self):
        scope = Mxo(self.cfg, FakeVisa(self.cfg))
        scope.prepare(self.path)
        scope.settings["acquisition"]["record_length"] = "50002"
        scope.acquire()
        with self.assertRaisesRegex(ValueError, "prepared=50002, header=50000"):
            scope.export(self.path, {})
        self.assertFalse((self.path / "waveforms.h5").exists())
        scope.close()

    def test_worker_handshake(self):
        atomic_json(self.path / "context.json", {"Case": {}, "Readback": {}})
        codes = []
        thread = threading.Thread(target=lambda: codes.append(worker(self.cfg, self.path,
            lambda cfg: Mxo(cfg, FakeVisa(cfg)))))
        thread.start()
        try:
            for state, command in (("prepared", "acquire"), ("frozen", "export"), ("complete", None)):
                deadline = time.monotonic()+10
                while True:
                    p = self.path / "status.json"
                    actual = read_json(p)["state"] if p.exists() else "starting"
                    self.assertNotEqual(actual, "failed")
                    if actual == state:
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                if command:
                    atomic_json(self.path / (command+".json"), {})
        finally:
            atomic_json(self.path / "abort.json", {})
            thread.join(5)
        self.assertEqual(codes, [0])
        self.assertEqual(read_json(self.path / "manifest.json")["analysis_status"], "complete")

    def test_worker_failure_and_abort(self):
        for action, fault in (("acquire", "timeout"), ("abort", "")):
            p = self.path / action
            p.mkdir()
            atomic_json(p / (action+".json"), {})
            code = worker(self.cfg, p, lambda cfg: Mxo(cfg, FakeVisa(cfg, fault)))
            self.assertEqual(code, 1)
            self.assertEqual(read_json(p / "status.json")["state"], "failed")
            self.assertFalse((p / "waveforms.h5").exists())

    def test_query_recovers_from_a_pending_response(self):
        # A VISA timeout leaves an unread response behind; clear() releases it and the
        # operation must succeed on retry instead of poisoning the rest of the batch.
        self.cfg["instrument"]["settle_after_clear_s"] = 0
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        visa.fail_next = 1
        self.assertEqual(scope.query("CHANnel1:STATe?"), "0")
        self.assertEqual(visa.clears, 1)
        self.assertEqual(scope.query("CHANnel1:STATe?"), "0")
        self.assertEqual(visa.clears, 1)

    def test_unrecoverable_link_reports_the_power_cycle_requirement(self):
        self.cfg["instrument"]["settle_after_clear_s"] = 0
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        visa.dead = True
        with self.assertRaises(RuntimeError) as caught:
            scope.query("*IDN?")
        self.assertIn("Power-cycle", str(caught.exception))
        self.assertIn("*IDN?", str(caught.exception))

    def test_payload_recovers_and_is_length_checked(self):
        self.cfg["instrument"]["settle_after_clear_s"] = 0
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        scope.settings = {"acquisition": {"resolution_s": "0.00001", "record_length": "50000"}}
        visa.fail_next = 1
        values = scope.read_payload("CHANnel1:DATA:VALues?", 50000 * 4)
        self.assertEqual(len(values), 50000)
        self.assertEqual(visa.clears, 1)

    def test_payload_budget_covers_the_whole_transfer(self):
        self.cfg["instrument"]["export_timeout_s"] = 1
        self.cfg["instrument"]["min_throughput_bytes_per_s"] = 2048
        scope = Mxo(self.cfg, FakeVisa(self.cfg))
        # The configured timeout is a floor: a payload too small to need longer gets it.
        self.assertEqual(scope._transfer_timeout_ms(128), 1000)
        # Above that the budget scales with the payload, in milliseconds.
        self.assertEqual(scope._transfer_timeout_ms(4096), 4000)
        # A large payload is covered end to end at the assumed floor rate, so a long
        # record is not cut off mid-transfer (which is what breaks the link).
        mebibyte = 1 << 20
        # 2 MiB at 2048 B/s is 1024 s of transfer; doubled for margin that is 2048 s.
        self.assertEqual(scope._transfer_timeout_ms(2 * mebibyte), round(2000.0 * 2 * mebibyte / 2048))
        self.assertGreater(scope._transfer_timeout_ms(64 * mebibyte), 60000)


    def test_snapshot_tolerates_a_query_that_never_answers(self):
        # A recovered query is invisible to the caller; only a command that keeps
        # failing is recorded as unavailable, and it must not fail the whole case.
        self.cfg["instrument"]["settle_after_clear_s"] = 0
        self.cfg["instrument"]["query_attempts"] = 1
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        visa.dead = True

        def drop_only_resolution(command):
            if command == "ACQuire:RESolution?":
                raise VisaIOError(-1073807339)
            visa.dead = False
            try:
                return FakeVisa.query(visa, command)
            finally:
                visa.dead = True

        visa.query = drop_only_resolution
        info = scope.snapshot()
        self.assertIn("ACQuire:RESolution?", info["unavailable"])
        self.assertEqual(info["channels"]["CH1"]["unit"], "A")

        # A probe unit that cannot be read must be refused: the waveform would be
        # stored without a measurement unit.
        visa = FakeVisa(self.cfg)
        scope = Mxo(self.cfg, visa)
        visa.dead = True

        def drop_only_unit(command):
            if command == "PROBe1:SETup:ATTenuation:UNIT?":
                raise VisaIOError(-1073807339)
            visa.dead = False
            try:
                return FakeVisa.query(visa, command)
            finally:
                visa.dead = True

        visa.query = drop_only_unit
        with self.assertRaises(RuntimeError) as caught:
            scope.snapshot()
        self.assertIn("probe unit", str(caught.exception))

    def test_analysis_skipped_for_a_unitless_channel(self):
        # A scope-only channel has no unit, so no current-mode spectrum is defined;
        # the capture must still complete with its waveform metadata.
        self.cfg["analysis"]["channel"] = "CH1"
        self.cfg["channels"]["CH1"]["unit"] = None
        p = self.path / "unitless"
        p.mkdir()
        atomic_json(p / "acquire.json", {})
        atomic_json(p / "export.json", {})
        atomic_json(p / "context.json", {"Case": {"Id": "unitless"}})
        code = worker(self.cfg, p, lambda cfg: Mxo(cfg, FakeVisa(cfg)))
        self.assertEqual(code, 0)
        manifest = read_json(p / "manifest.json")
        self.assertEqual(manifest["analysis_status"], "skipped")
        self.assertIn("no configured unit", manifest["analysis_skipped_reason"])


if __name__ == "__main__":
    unittest.main()
