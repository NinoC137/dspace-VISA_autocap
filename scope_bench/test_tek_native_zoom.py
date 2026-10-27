"""Offline fake Tek: verify screenshot SCPI, event positions, PNGs and restore."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import h5py
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from screenshots import _tek_screenshot
except ModuleNotFoundError:
    sys.path.insert(1, str(Path(__file__).resolve().parents[3]/
                           "controldesk_automation"/"scope_bench"))
    from screenshots import _tek_screenshot


class FakeTek:
    backend = "tek"

    def __init__(self, share, policy):
        self.config = {
            "screenshots": {"enabled": True, "spans_s": [.2] if policy != "steady_best" else [.2, .01],
                            "center_fraction": .5, "settle_s": 0},
            "instrument": {"remote": {"ssh_executable": "ssh", "host": "fake-pi",
                                      "share_root": "/fake-share", "local_share_root": str(share)}},
        }
        self.values = {"DISplay:WAVEView1:ZOOM:ZOOM1:STATe": "0",
                       "DISplay:WAVEView1:ZOOM:ZOOM1:HORizontal:POSition": "50",
                       "DISplay:WAVEView1:ZOOM:ZOOM1:HORizontal:WINSCALe": "1"}
        self.writes = []

    def query(self, command):
        if command == "ACQuire:STATE?":
            return "0"
        if command == "ACQuire:NUMACq?":
            return "5"
        if command == "HORizontal:POSition?":
            return "0"
        if command == "*OPC?":
            return "1"
        return self.values[command.removesuffix("?")]

    def write(self, command):
        key, value = command.rsplit(" ", 1)
        self.values[key] = "1" if value == "ON" else "0" if value == "OFF" else value
        self.writes.append(command)

    def errors(self):
        return []


def run_one(root, policy, fail_zoom=False):
    capture = root/policy/"capture_1"
    capture.mkdir(parents=True)
    share = root/policy/"share"
    share.mkdir()
    raw = capture/"waveforms.h5"
    with h5py.File(raw, "w") as h:
        ch = h.create_group("channels/CH1")
        ch.attrs["N"] = 100001
        ch.attrs["dt_s"] = .0001
        ch.attrs["t0_s"] = 0.
    base = datetime(2026, 10, 9, tzinfo=timezone.utc)
    (capture/"events.jsonl").write_text(json.dumps({"state": "acquiring", "utc": base.isoformat()})+"\n")
    events = [] if policy == "steady_best" else [
        {"action": "xi_switch", "xi": xi, "utc": (base+timedelta(seconds=t)).isoformat()}
        for t, xi in ((3.5, .515), (6.5, .8))]
    case = {"Case": {"Xi": .515 if policy == "steady_best" else .4, "Note": policy},
            "Events": events}
    scope = FakeTek(share, policy)
    next_png = 0

    def fake_run(argv, **kwargs):
        nonlocal next_png
        next_png += 1
        if fail_zoom and next_png == 2:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fake screenshot failure")
        prefix = argv[-1].rsplit("-f ", 1)[-1]
        Image.new("RGB", (800, 480), "#203040").save(share/f"{prefix}_{next_png}.png")
        return subprocess.CompletedProcess(argv, 0, stdout="saved", stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        result = _tek_screenshot(scope, capture, case, raw)
    assert result["status"] == ("failed" if fail_zoom else "complete"), result
    assert len(result["images"]) == (1 if fail_zoom else 3)
    assert all((capture/item["file"]).is_file() for item in result["images"])
    assert result["images"][0]["file"] == "screen.png"
    if policy != "steady_best" and not fail_zoom:
        assert [round(item["actual_position_percent"]) for item in result["images"][1:]] == [35, 65]
        assert all(.15 <= item["actual_span_s"] <= .3 for item in result["images"][1:])
    assert scope.values["DISplay:WAVEView1:ZOOM:ZOOM1:STATe"] == "0"
    assert float(scope.values["DISplay:WAVEView1:ZOOM:ZOOM1:HORizontal:POSition"]) == 50
    assert float(scope.values["DISplay:WAVEView1:ZOOM:ZOOM1:HORizontal:WINSCALe"]) == 1
    assert (capture/"screenshots"/"index.json").is_file()
    if fail_zoom:
        assert "fake screenshot failure" in result["error"]


def main():
    with tempfile.TemporaryDirectory(prefix="tek_native_zoom_fake_") as temp:
        root = Path(temp)
        run_one(root, "fixed90")
        run_one(root, "steady_best")
        run_one(root, "adaptive", fail_zoom=True)
    print("OFFLINE NATIVE ZOOM OK: event views, steady views, PNGs, success/failure restore")


if __name__ == "__main__":
    main()
