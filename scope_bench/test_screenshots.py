import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from bench import worker
from common import atomic_json, load_config, read_json, parse_spans, validate
from mxo import Mxo
from screenshots import capture_screenshots, checked_png
from test_bench import FakeVisa

HERE = Path(__file__).parent


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (320, 180), "#24354a").save(stream, format="PNG")
    return stream.getvalue()


class DisplayVisa(FakeVisa):
    def __init__(self, config, fault=""):
        super().__init__(config, fault)
        self.layout = 1
        self.enabled_layouts = {1}
        self.display = {"SYSTem:DISPlay:UPDate": "0", "DISPlay:DIAGram:GRID": "0",
                        "DISPlay:DIAGram:LABels": "0", "DISPlay:DIAGram:XFIXed": "0"}
        self.shots = 0
        self.zoom = {}

    def query(self, command):
        if command == "STATus:OPERation:CONDition?":
            return "16" if self.fault == "running" else "96"
        if command == "LAYout:ACTive?":
            return '"'+str(self.layout)+'"'
        if command[:-1] in self.display:
            return self.display[command[:-1]]
        if command.startswith("LAYout"):
            if command.endswith(":ENABle?"):
                return str(int(int(command[6]) in self.enabled_layouts))
            if command.endswith(":DIAG?"):
                return '"9"'
            if command.endswith((":SPAN?", ":POSition?")):
                return self.zoom[command[:-1]]
        if command.endswith(":DATA:HEADer?") and self.fault == "changed" and self.shots:
            return "0,0.24999,25000,1"
        return super().query(command)

    def write(self, command):
        super().write(command)
        key, _, value = command.partition(" ")
        if (key == "LAYout:ACTive" or key.endswith(":DIAGram1:SOURce")) and '"' in value:
            raise ValueError("SCPI -158 String data not allowed")
        value = value.strip('"')
        if key == "LAYout:ACTive":
            self.layout = int(value)
        elif key in self.display:
            self.display[key] = {"ON": "1", "OFF": "0"}.get(value, value)
        elif key.startswith("LAYout") and key.endswith(":ENABle") and ":ZOOM" not in key:
            i = int(key[6])
            if value in ("1", "ON"):
                self.enabled_layouts.add(i)
                self.layout = i
            else:
                self.enabled_layouts.discard(i)
        elif ":HORizontal:ABSolute:" in key:
            self.zoom[key] = value

    def screenshot_bytes(self):
        self.shots += 1
        if self.fault == "third_png" and self.shots == 3:
            return b"not a PNG"
        return png_bytes()


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(HERE/"bench_config.json")
        # These tests drive the MXO backend with a fake VISA session, so pin the
        # instrument identity here: the deployed bench_config.json is intentionally
        # swappable (switch-scope.cmd), and may well point at another oscilloscope.
        self.cfg["instrument"].update({
            "resource": "USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR",
            "expected_model": "MXO38",
            "expected_serial": "1335.2050k08/101400",
            "transport": "local_visa"})
        for name, item in self.cfg["channels"].items():
            item["enabled"] = name in ("CH1", "CH8")
        self.cfg["analysis"]["make_plots"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.axis = dict(t0_s=0., dt_s=1e-5, N=50000)

    def capture(self, fault=""):
        session = DisplayVisa(self.cfg, fault)
        scope = Mxo(self.cfg, session=session)
        with patch("screenshots.time.sleep"):
            report = capture_screenshots(scope, self.path, {"Case": {"Id": "case_01", "VL": 120}}, self.axis)
        return session, report

    def test_five_native_images_correct_spans_restored_no_acquisition(self):
        session, report = self.capture()
        self.assertEqual(report["status"], "complete", report)
        self.assertEqual(len(report["images"]), 5)
        self.assertEqual([e["actual_span_s"] for e in report["images"]], self.cfg["screenshots"]["spans_s"])
        self.assertEqual(session.layout, 1)
        self.assertEqual(session.enabled_layouts, {1})
        self.assertEqual(set(session.display.values()), {"0"})
        self.assertFalse(any(c.startswith(("RUN", "STOP", "TIMebase", "ACQuire", "CHANnel")) for c in session.writes))
        for entry in report["images"]:
            raw = self.path/"screenshots"/entry["raw"]
            self.assertEqual(raw.read_bytes(), png_bytes())
            with Image.open(self.path/"screenshots"/entry["marked"]) as marked:
                self.assertEqual(marked.crop((0,0,320,180)).tobytes(), checked_png(png_bytes()).tobytes())

    def test_failure_keeps_completed_pngs_and_restores_layout(self):
        session, report = self.capture("third_png")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["images"]), 2)
        self.assertEqual(session.enabled_layouts, {1})
        self.assertEqual(set(session.display.values()), {"0"})
        self.assertEqual(read_json(self.path/"screenshots/index.json")["status"], "failed")

    def test_running_or_changed_record_is_rejected(self):
        for fault in ("running", "changed"):
            session, report = self.capture(fault)
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["images"], [])
            self.assertEqual(session.layout, 1)

    def test_disabled_makes_no_instrument_calls(self):
        self.cfg["screenshots"]["enabled"] = False
        session, report = self.capture()
        self.assertEqual(report["status"], "disabled")
        self.assertFalse(session.writes or session.reads)

    def test_worker_retains_analysis_when_screenshot_fails(self):
        atomic_json(self.path/"context.json", {})
        session = DisplayVisa(self.cfg, "third_png")
        with patch("screenshots.time.sleep"):
            code = worker(self.cfg, self.path, lambda cfg: Mxo(cfg, session=session), standalone=True)
        self.assertEqual(code, 0)
        manifest = read_json(self.path/"manifest.json")
        self.assertEqual(manifest["capture_status"], "complete")
        self.assertEqual(manifest["analysis_status"], "complete")
        self.assertEqual(manifest["screenshots"]["status"], "failed")
        self.assertTrue(read_json(self.path/"status.json")["warnings"])

    def test_span_input_and_out_of_record_validation(self):
        self.assertEqual(parse_spans("200ms,50 ms,10ms,1ms,100µs"), [0.2,0.05,0.01,0.001,0.0001])
        for text in ("200", "-1ms", "nan s", "200ms/div"):
            with self.assertRaises(ValueError):
                parse_spans(text)
        self.cfg["screenshots"]["spans_s"] = [1]
        with self.assertRaisesRegex(ValueError, "超出"):
            validate(self.cfg)


if __name__ == "__main__":
    unittest.main()
