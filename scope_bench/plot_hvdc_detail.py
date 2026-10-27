"""Render a clean HVDC-current detail and FFT for each completed Tek capture.

The scope is started by software near 6 s after Enable1. Its 0.4 s HDF5 record
is not synchronized to the simulation clock. We use centered 100 ms and 40 ms
windows from that record and retain the timing distinction in index.json only.
"""

import argparse
import json
from pathlib import Path
import re
import sys

import h5py
import numpy as np


WINDOWS_S = (0.1, 0.04)
NOMINAL_START_S = 6.0
FFT_MAX_HZ = 50_000.0


def load_window(raw, window_s):
    with h5py.File(raw, "r") as handle:
        if handle.attrs.get("backend") != "tek":
            raise ValueError("not a Tek capture")
        if "channels/CH1" not in handle:
            raise ValueError("CH1 was not captured")
        config = json.loads(handle.attrs["config_json"])
        if abs(float(config["timing"]["settle_s"]) - NOMINAL_START_S) > 1e-9:
            raise ValueError("scope acquisition was not scheduled near 6 s")
        if float(config["timing"]["enable_duration_s"]) < NOMINAL_START_S + window_s:
            raise ValueError("Enable1 interval is shorter than the requested window")
        ch = handle["channels/CH1"]
        if ch.attrs["unit"] != "A":
            raise ValueError("CH1 is not stored in amperes")
        dt = float(ch.attrs["dt_s"])
        if not np.isfinite(dt) or dt <= 0:
            raise ValueError("invalid sample interval")
        dataset = ch["samples"]
        count = int(np.ceil(window_s / dt))
        if len(dataset) < count:
            raise ValueError("CH1 record does not contain the requested window")
        start = (len(dataset) - count) // 2
        current = np.asarray(dataset[start:start + count], dtype=np.float64)
        current -= float(config["channels"]["CH1"].get("zero_offset", 0.0))
        if not np.all(np.isfinite(current)):
            raise ValueError("CH1 contains nonfinite samples")
        return current, dt, start, len(dataset)


def fft_amplitude(current, dt):
    centered = current - np.mean(current)
    window = np.hanning(len(centered))
    spectrum = np.fft.rfft(centered * window)
    frequency = np.fft.rfftfreq(len(centered), d=dt)
    amplitude = 2.0 * np.abs(spectrum) / window.sum()
    amplitude[0] = 0.0  # DC was removed before the transform.
    if len(centered) % 2 == 0:
        amplitude[-1] *= 0.5
    return frequency, amplitude


def minmax_trace(current, dt, columns=2600):
    """Preserve peaks when 100 ms contains more cycles than image pixels."""
    from matplotlib.collections import LineCollection

    n = len(current)
    edges = np.linspace(0, n, min(columns, n) + 1, dtype=np.int64)
    edges = np.unique(edges)
    lo = np.minimum.reduceat(current, edges[:-1])
    hi = np.maximum.reduceat(current, edges[:-1])
    x_ms = 500.0 * (edges[:-1] + edges[1:] - 1) * dt
    segments = np.stack(
        (np.column_stack((x_ms, lo)), np.column_stack((x_ms, hi))), axis=1
    )
    return LineCollection(segments, colors="#155e9a", linewidths=0.7)


def render(current, dt, png, svg, window_s):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt

    frequency, amplitude = fft_amplitude(current, dt)
    fig, (wave, fft) = plt.subplots(2, 1, figsize=(16, 9), dpi=150,
                                    gridspec_kw={"height_ratios": [1.05, 1]},
                                    constrained_layout=True)
    wave.add_collection(minmax_trace(current, dt))
    wave.set_xlim(0, window_s * 1000)
    lower, upper = float(current.min()), float(current.max())
    if lower == upper:
        lower, upper = lower - 0.5, upper + 0.5
    wave.set_ylim(lower, upper)
    wave.set_xticks(np.linspace(0, window_s * 1000, 11))
    wave.margins(y=0.08)

    show = frequency <= FFT_MAX_HZ
    fft.plot(frequency[show] / 1000.0, amplitude[show], color="#ae4b35",
             linewidth=0.75)
    fft.set_xlim(0, FFT_MAX_HZ / 1000.0)
    fft.set_ylim(bottom=0)
    fft.set_xticks(np.linspace(0, FFT_MAX_HZ / 1000.0, 11))

    for axis in (wave, fft):
        axis.grid(True, color="#d7dce2", linewidth=0.65)
        axis.tick_params(labelsize=10, colors="#3b4652")
        for spine in axis.spines.values():
            spine.set_color("#97a1ad")
    # No titles, axis names, legends or peak annotations: the operator will label.
    fig.savefig(png, dpi=150, facecolor="white")
    fig.savefig(svg, facecolor="white")
    plt.close(fig)
    return {"sample_count": len(current), "sample_interval_s": dt,
            "fft_bin_hz": float(frequency[1] - frequency[0]),
            "fft_window": "Hann", "fft_dc_removed": True,
            "fft_amplitude": "single-sided peak A", "fft_display_max_hz": FFT_MAX_HZ}


def file_stem(capture):
    case = capture.parent.parent.name
    case = re.sub(r"[^A-Za-z0-9_.-]+", "_", case)
    return case + "_" + capture.name[-8:]


def process(batch):
    batch = batch.resolve()
    if not batch.is_dir():
        raise ValueError("batch directory does not exist")
    output = batch / "hvdc_detail"
    rows = []
    manifests = sorted(batch.rglob("manifest.json"))
    for manifest_path in manifests:
        capture = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if manifest.get("backend") != "tek":
                continue
            if manifest.get("capture_status") != "complete":
                raise ValueError("capture is not complete")
            raw = capture / manifest.get("file", "waveforms.h5")
            output.mkdir(exist_ok=True)
            stem = file_stem(capture)
            figures = []
            for window_s in WINDOWS_S:
                current, dt, start, total = load_window(raw, window_s)
                suffix = "_%dms" % round(window_s * 1000)
                png = output / (stem + suffix + ".png")
                svg = output / (stem + suffix + ".svg")
                if png.exists() != svg.exists():
                    raise FileExistsError("only one detail figure exists: " + stem + suffix)
                if png.exists():
                    state = "existing"
                    metadata = {"sample_count": len(current), "sample_interval_s": dt,
                                "fft_bin_hz": 1.0 / (len(current) * dt),
                                "fft_window": "Hann", "fft_dc_removed": True,
                                "fft_amplitude": "single-sided peak A",
                                "fft_display_max_hz": FFT_MAX_HZ}
                else:
                    temporary_png = output / (stem + suffix + ".new.png")
                    temporary_svg = output / (stem + suffix + ".new.svg")
                    try:
                        metadata = render(current, dt, temporary_png, temporary_svg, window_s)
                        temporary_png.replace(png)
                        temporary_svg.replace(svg)
                    finally:
                        temporary_png.unlink(missing_ok=True)
                        temporary_svg.unlink(missing_ok=True)
                    state = "complete"
                figures.append({"window_s": window_s, "status": state,
                                "png": str(png.relative_to(batch)),
                                "svg": str(svg.relative_to(batch)),
                                "record_samples": total, "window_first_sample": start,
                                "window_start_in_record_s": start * dt, **metadata})
            rows.append({"capture": str(capture.relative_to(batch)), "status": "complete",
                         "figures": figures})
        except Exception as exc:
            rows.append({"capture": str(capture.relative_to(batch)), "status": "skipped",
                         "reason": str(exc)})
    if not rows:
        print("HVDC detail: no Tek captures in batch")
        return 0
    index = {
        "definition": "middle 100 ms and 40 ms of CH1 scope acquisition started near simulation time 6 s",
        "simulation_start_s": NOMINAL_START_S,
        "windows_s": WINDOWS_S,
        "time_alignment": "approximate; scope trigger is not synchronized to simulation clock",
        "selection": "both windows centered in the saved CH1 record; 40 ms lies within 100 ms",
        "waveform_x": "ms after first sample of the selected middle window",
        "waveform_y": "A, stored CH1 with configured zero offset removed",
        "fft_x": "kHz", "fft_y": "single-sided peak A after DC removal and Hann window",
        "plots": rows,
    }
    output.mkdir(exist_ok=True)
    (output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    done = sum(row["status"] in ("complete", "existing") for row in rows)
    skipped = len(rows) - done
    print(f"HVDC detail: {done} complete, {skipped} skipped; {output}")
    return 0 if skipped == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    try:
        return process(args.batch)
    except Exception as exc:
        print("HVDC detail failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
