"""End-to-end tests for scope_api.py: real subprocesses, real CLI, fake instrument.

Each subcommand is run as its own process, exactly as an agent or the MCP server would,
and its single JSON result is checked. The instrument is faked, so this never touches
hardware and can run on the rig or anywhere the tree is deployed.
"""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = HERE / "scope_api.py"
FAKE_WORKER = HERE / "test_api_fake_worker.py"
PYTHON = sys.executable


def run_api(*args, timeout=120):
    """Run one scope_api subcommand and return (json, returncode, stderr)."""
    result = subprocess.run([PYTHON, "-u", str(API), *args],
                            capture_output=True, text=True, timeout=timeout)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"no JSON on stdout: rc={result.returncode} stderr={result.stderr[-800:]}")
    return json.loads(lines[-1]), result.returncode, result.stderr


class ScopeApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        def cleanup():
            # The final status may arrive just before the detached process releases
            # worker.log on Windows. Wait for teardown, still fail if it stays open.
            deadline = time.monotonic() + 5
            while True:
                try:
                    self.tmp.cleanup()
                    return
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        self.addCleanup(cleanup)
        self.root = Path(self.tmp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        self.work = self.root / "work"

    def start_capture(self, *extra):
        prepared, code, _ = run_api("capture", "--directory", str(self.results), *extra)
        self.assertEqual(code, 0, prepared)
        self.assertTrue(prepared["ok"], prepared)
        started, code, _ = run_api("capture-start", "--directory", prepared["directory"],
                                   "--workdir", str(self.work), "--worker", str(FAKE_WORKER))
        self.assertEqual(code, 0, started)
        self.assertTrue(started["ok"], started)
        return prepared

    def wait_for(self, directory, timeout=60):
        """Poll capture-status the way an agent would, returning the final payload."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last, code, _ = run_api("capture-status", "--directory", directory)
            self.assertEqual(code, 0, last)
            if last.get("state") in ("complete", "failed"):
                return last
            time.sleep(0.2)
        self.fail(f"capture did not finish: {last}")

    def test_ping_reports_a_link_failure_as_json(self):
        result, code, _ = run_api("ping")
        # Without hardware this must still answer with one parseable JSON object.
        self.assertIn("ok", result)
        self.assertIsInstance(result["ok"], bool)

    def test_captures_lists_an_empty_root(self):
        result, code, _ = run_api("captures", "--directory", str(self.results))
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "count": 0, "captures": []})

    def test_capture_rejects_a_unitless_channel(self):
        result, code, _ = run_api("capture", "--directory", str(self.results), "--channels", "CH3")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("no measurement unit", result["error"])

    def test_capture_rejects_an_unknown_channel(self):
        result, code, _ = run_api("capture", "--directory", str(self.results),
                                  "--channels", "CH9", "--unit", "V")
        self.assertEqual(code, 1)
        self.assertIn("Unknown channel", result["error"])

    def test_capture_rejects_a_too_short_record(self):
        result, code, _ = run_api("capture", "--directory", str(self.results),
                                  "--channels", "CH1", "--record-duration-s", "0.05")
        self.assertEqual(code, 1)
        self.assertIn("Welch", result["error"])

    def test_interlock_refusal_blocks_the_capture(self):
        before = list(self.results.iterdir())
        result, code, _ = run_api("capture", "--directory", str(self.results),
                                  "--channels", "CH1", "--interlock", "exit 7")
        self.assertEqual(code, 1)
        self.assertIn("Interlock refused", result["error"])
        self.assertEqual(list(self.results.iterdir()), before,
                         "a refused capture must not create a capture directory")

    def test_interlock_success_allows_the_capture(self):
        result, code, _ = run_api("capture", "--directory", str(self.results),
                                  "--channels", "CH1", "--interlock", "exit 0")
        self.assertEqual(code, 0)
        self.assertTrue(result["started"])
        # The interlock is only a gate: nothing is captured by preparing.
        self.assertFalse((Path(result["directory"]) / "status.json").exists())

    def test_raw_refuses_writes(self):
        for command in ("CHANnel2:STATe 0", "*RST", "*CLS"):
            result, code, _ = run_api("raw", "--command", command)
            self.assertEqual(code, 1, command)
            self.assertFalse(result["ok"], command)

    def test_full_capture_completes_with_a_manifest_and_waveform(self):
        prepared = self.start_capture("--channels", "CH1", "--label", "e2e")
        final = self.wait_for(prepared["directory"])
        self.assertEqual(final["state"], "complete", final)
        directory = Path(prepared["directory"])
        self.assertTrue((directory / "waveforms.h5").exists())
        self.assertTrue((directory / "worker.log").exists(),
                        "the worker log must live beside the capture, not in a temp dir")
        manifest = final["manifest"]
        self.assertEqual(manifest["capture_status"], "complete")
        self.assertEqual(manifest["analysis_status"], "complete")
        self.assertEqual(manifest["channels"], ["CH1"])
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertIsNotNone(manifest["metrics"])
        self.assertIn("link_errors", manifest["link"])

    def test_capture_status_reports_the_event_sequence(self):
        prepared = self.start_capture("--channels", "CH1")
        self.wait_for(prepared["directory"])
        events = (Path(prepared["directory"]) / "events.jsonl").read_text(encoding="utf-8")
        states = [json.loads(line)["state"] for line in events.splitlines() if line.strip()]
        self.assertEqual(states[0], "starting")
        for expected in ("prepared", "acquiring", "frozen", "exporting", "complete"):
            self.assertIn(expected, states)
        self.assertEqual(states.index("acquiring") < states.index("frozen") < states.index("exporting"),
                         True)

    def test_captures_summarises_a_finished_capture(self):
        prepared = self.start_capture("--channels", "CH1")
        self.wait_for(prepared["directory"])
        result, code, _ = run_api("captures", "--directory", str(self.results))
        self.assertEqual(code, 0)
        self.assertEqual(result["count"], 1)
        item = result["captures"][0]
        self.assertEqual(item["capture_status"], "complete")
        self.assertEqual(item["channels"], ["CH1"])
        self.assertEqual(len(item["sha256"]), 64)

    def test_abort_before_the_worker_starts_is_reported(self):
        prepared, code, _ = run_api("capture", "--directory", str(self.results),
                                    "--channels", "CH1")
        self.assertEqual(code, 0)
        result, code, _ = run_api("capture-abort", "--directory", prepared["directory"],
                                  "--workdir", str(self.work))
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertTrue((Path(prepared["directory"]) / "abort.json").exists())

    def test_abort_of_a_missing_capture_fails_cleanly(self):
        result, code, _ = run_api("capture-abort", "--directory",
                                  str(self.results / "does-not-exist"), "--workdir", str(self.work))
        self.assertEqual(code, 1)
        self.assertIn("No such capture directory", result["error"])

    def test_second_start_refuses_to_reuse_a_capture_directory(self):
        prepared = self.start_capture("--channels", "CH1")
        self.wait_for(prepared["directory"])
        again, code, _ = run_api("capture-start", "--directory", prepared["directory"],
                                 "--workdir", str(self.work), "--worker", str(FAKE_WORKER))
        self.assertEqual(code, 1)
        self.assertIn("already used", again["error"])
        final = self.wait_for(prepared["directory"])
        self.assertEqual(final["state"], "complete")

    def test_unit_override_labels_the_waveform(self):
        prepared = self.start_capture("--channels", "CH3", "--unit", "V")
        final = self.wait_for(prepared["directory"])
        self.assertEqual(final["state"], "complete", final)
        # CH3 has no unit in bench_config.json; the override must make it capturable
        # and must be recorded in the worker's own settings file.
        settings = json.loads((Path(prepared["directory"]) / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["channels"]["CH3"]["unit"], "V")


if __name__ == "__main__":
    unittest.main()
