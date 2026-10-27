#!/usr/bin/env python3
"""Inspect the end-to-end capture: waveform structure + analysis metrics."""
import json
from pathlib import Path

import h5py

OUT = Path(r"F:\ZYS_HYBASE\automation\scope_results\tek_bench_e2e")

print("=== settings.json (instrument side) ===")
settings = json.loads((OUT / "settings.json").read_text(encoding="utf-8"))
print(" identity :", settings["identity"]["model"], settings["identity"]["serial"],
      settings["identity"]["firmware"])
print(" acquisition:", json.dumps(settings["acquisition"], ensure_ascii=False))
print(" timebase :", json.dumps(settings["timebase"], ensure_ascii=False))
print(" trigger  :", json.dumps(settings["trigger"], ensure_ascii=False))
print(" CH2      :", json.dumps(settings["channels"]["CH2"], ensure_ascii=False))

print("\n=== waveforms.h5 ===")
with h5py.File(OUT / "waveforms.h5", "r") as handle:
    print(" root attrs:", {k: handle.attrs[k] for k in handle.attrs if k != "config_json"})
    for name, group in handle["channels"].items():
        samples = group["samples"]
        attrs = {k: group.attrs[k] for k in
                 ("N", "dt_s", "t0_s", "xstop_s", "unit", "signal",
                  "instrument_yunit", "volts_per_unit") if k in group.attrs}
        print(" %s: shape=%s dtype=%s" % (name, samples.shape, samples.dtype))
        print("    attrs:", json.dumps(attrs, ensure_ascii=False, default=str))
        import numpy as np
        data = samples[:]
        print("    min=%.6g max=%.6g mean=%.6g" % (data.min(), data.max(), data.mean()))

print("\n=== analysis metrics.json ===")
metrics_path = OUT / json.loads((OUT / "manifest.json").read_text(encoding="utf-8"))["metrics"]
report = json.loads(Path(metrics_path).read_text(encoding="utf-8"))
print(" status  :", report.get("analysis_status"), "| quality:", report.get("quality_status"))
print(" warnings:", report.get("warnings"))
for channel, values in report.get("channels", {}).items():
    keys = ("unit", "samples", "sample_rate_hz", "duration_s", "dc", "rms", "rms_ac",
            "minimum", "maximum", "peak_to_peak", "two_group_rms_A",
            "welch_df_hz", "stationary")
    print(" %s: %s" % (channel, json.dumps({k: values.get(k) for k in keys},
                                           ensure_ascii=False, default=str)))
    for order in ("1", "2"):
        group = values.get("groups", {}).get(order, {}).get("group")
        if group:
            print("    group %s: %.1f-%.1f Hz  rms=%.6g A" % (
                order, group["low_hz"], group["high_hz"], group["rms_A"]))
print("\n analysis dir:", Path(metrics_path).parent)
