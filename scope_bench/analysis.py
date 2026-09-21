"""Reproducible finite-record RMS and carrier-group PSD integration."""
import csv
import importlib.metadata
import json
from pathlib import Path
import uuid

import h5py
import numpy as np
from scipy import signal

from common import atomic_json, read_json, sha256

VERSION = "1.0.0"


def basic_metrics(samples):
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim != 1 or len(x) < 2 or not np.all(np.isfinite(x)):
        raise ValueError("Expected a finite scalar waveform.")
    dc = float(np.mean(x))
    ac2 = float(np.mean((x - dc) ** 2))
    return dict(dc=dc, rms=float(np.sqrt(np.mean(x * x))), rms_ac=float(np.sqrt(ac2)),
                minimum=float(x.min()), maximum=float(x.max()), peak_to_peak=float(np.ptp(x)),
                ripple_factor=float(np.sqrt(ac2) / abs(dc)) if abs(dc) > 1e-12 else None)


def integrate_band(f, psd, low, high):
    df = f[1] - f[0]
    if not 0 < low < high < f[-1]:
        raise ValueError("Band must lie strictly inside the measured Nyquist interval.")
    widths = np.maximum(0, np.minimum(f + df / 2, high) - np.maximum(f - df / 2, low))
    return float(np.dot(psd, widths))


def current_metrics(samples, fs, config):
    x = np.asarray(samples, dtype=np.float64)
    result = basic_metrics(x)
    a = config["analysis"]
    if not np.isfinite(fs) or fs <= 0:
        raise ValueError("Invalid sample rate.")
    nperseg = round(a["welch_segment_s"] * fs)
    if nperseg < 8 or len(x) < 2 * nperseg:
        raise ValueError("Record too short for configured Welch analysis.")
    centered = x - result["dc"]
    f, psd = signal.welch(centered, fs, window="hann", nperseg=nperseg,
                          noverlap=int(nperseg * a["overlap"]), detrend=False, scaling="density")
    ff, full = signal.periodogram(centered, fs, window="hann", detrend=False, scaling="density")
    groups = {}
    for order in (1, 2):
        center = order * a["fsw_hz"]
        group = {}
        for tag, width in (("strict", a["q_max"] * a["f0_hz"]),
                           ("group", (a["q_max"] + 0.5) * a["f0_hz"])):
            if width == 0:
                group[tag] = None
                continue
            low, high = center - width, center + width
            mean_square = integrate_band(f, psd, low, high)
            group[tag] = dict(low_hz=low, high_hz=high, mean_square_A2=mean_square,
                              rms_A=float(np.sqrt(mean_square)),
                              full_record_mean_square_A2=integrate_band(ff, full, low, high))
        groups[str(order)] = group
    blocks = [basic_metrics(x[start:start + nperseg]) for start in range(0, len(x)-nperseg+1, nperseg)]
    dc_span = float(np.ptp([b["dc"] for b in blocks]))
    ac_values = [b["rms_ac"] for b in blocks]
    ac_span = float(np.ptp(ac_values))
    stationary = (dc_span <= max(a["dc_span_abs"], a["dc_span_relative"] * abs(result["dc"])) and
                  ac_span <= max(1e-12, a["ac_span_relative"] * np.mean(ac_values)))
    total = sum(g["group"]["mean_square_A2"] for g in groups.values())
    w = signal.get_window("hann", len(x))
    one_sided = np.full(len(ff), 2.0)
    one_sided[0] = 1
    if len(x) % 2 == 0:
        one_sided[-1] = 1
    amplitude = np.sqrt(full * fs * np.dot(w, w) * one_sided) / w.sum()
    result.update(groups=groups, two_group_rms_A=float(np.sqrt(total)),
                  two_group_mean_square_A2=total, two_group_A2s=total * len(x)/fs,
                  duration_s=len(x)/fs, sample_rate_hz=fs, samples=len(x),
                  welch_df_hz=float(f[1]-f[0]), window="periodic_hann",
                  parseval_spectral_A2=float(full.sum()*(ff[1]-ff[0])),
                  parseval_windowed_time_A2=float(np.dot(w*w, centered*centered)/np.dot(w, w)),
                  stationary=bool(stationary), dc_block_span=dc_span, ac_block_span=ac_span,
                  rms_band_definition="entire_acquisition_chain; no additional lowpass")
    return result, {"frequency_hz": f, "psd_A2_per_Hz": psd,
                    "full_frequency_hz": ff, "full_psd_A2_per_Hz": full,
                    "full_fft_peak_amplitude_A": amplitude}


def analyze_file(raw_path, config=None):
    raw_path = Path(raw_path)
    # A new result directory prevents offline reanalysis from overwriting earlier results.
    out = raw_path.parent / "analysis" / (VERSION + "_" + uuid.uuid4().hex[:12])
    out.mkdir(parents=True)
    report = {"version": VERSION, "raw_file": str(raw_path.resolve()), "sha256": sha256(raw_path),
              "channels": {}, "warnings": [], "analysis_status": "complete"}
    report["dependencies"] = {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "h5py")}
    with h5py.File(raw_path, "r") as handle:
        stored = json.loads(handle.attrs["config_json"])
        config = config or stored
        report["config"] = config
        report["case"] = json.loads(handle.attrs.get("case_json", "{}"))
        for key in ("anti_alias_verified", "probe_bandwidth_verified"):
            if not stored["acquisition"][key]:
                report["warnings"].append(key + "=false")
        target = config["analysis"]["channel"]
        if target not in handle["channels"]:
            report["current_analysis"] = "skipped: channel disabled/not recorded"
        for name, group in handle["channels"].items():
            y = group["samples"][:].astype(np.float64)
            channel = stored["channels"][name]
            y -= channel["zero_offset"]
            metrics = basic_metrics(y)
            unit = group.attrs["unit"]
            metrics["unit"] = unit
            if name == target:
                if unit != "A":
                    raise ValueError("Current analysis requires measured unit A.")
                metrics, spectrum = current_metrics(y, 1/group.attrs["dt_s"], config)
                metrics["unit"] = unit
                if not metrics["stationary"]:
                    report["warnings"].append(name + ": nonstationary")
                if not channel.get("zero_calibrated", False):
                    report["warnings"].append(name + ": zero offset not calibrated")
                if channel.get("probe_range_A") and np.max(np.abs(y)) >= channel["probe_range_A"]:
                    report["warnings"].append(name + ": probe range reached")
                with h5py.File(out / "spectrum.h5", "w") as spec:
                    for key, value in spectrum.items():
                        spec.create_dataset(key, data=value)
                if config["analysis"]["make_plots"]:
                    plot(y, group.attrs["dt_s"], spectrum, metrics, out)
            report["channels"][name] = metrics
    report["quality_status"] = "review" if report["warnings"] else "pass"
    atomic_json(out / "metrics.json", report)
    return out / "metrics.json"


def plot(y, dt, spectrum, metrics, out):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(10, 9), constrained_layout=True)
    stride = max(1, len(y)//20000)
    axes[0].plot(np.arange(0, len(y), stride)*dt, y[::stride], linewidth=0.5)
    axes[0].set(xlabel="Time (s)", ylabel="Current (A)", title="Display subsampled; metrics use all samples")
    f, p = spectrum["frequency_hz"], spectrum["psd_A2_per_Hz"]
    for order, ax in zip((1, 2), axes[1:]):
        band = metrics["groups"][str(order)]["group"]
        mask = (f >= band["low_hz"]-100) & (f <= band["high_hz"]+100)
        ax.semilogy(f[mask], np.maximum(p[mask], np.finfo(float).tiny))
        ax.axvspan(band["low_hz"], band["high_hz"], color="green", alpha=0.1)
        ax.set(xlabel="Frequency (Hz)", ylabel="PSD (A^2/Hz)", title=f"Group {order}: {band['rms_A']:.6g} A RMS")
    fig.savefig(out / "ch1_report.png", dpi=150)
    plt.close(fig)


def summarize(root):
    root = Path(root)
    rows = []
    for manifest in sorted(root.rglob("manifest.json")):
        m = read_json(manifest)
        if m.get("capture_status") != "complete" or not m.get("metrics"):
            continue
        report = read_json(manifest.parent / m["metrics"])
        row = {"capture": str(manifest.parent), "quality": report["quality_status"]}
        case = report.get("case", {})
        row.update({k: case.get("Case", {}).get(k) for k in ("Id", "VL")})
        row.update({k: case.get("Readback", {}).get(k) for k in ("Xi", "Phi", "Ud6", "Ud7")})
        for name, mtr in report["channels"].items():
            for field in ("unit", "dc", "rms", "rms_ac", "two_group_rms_A", "two_group_mean_square_A2"):
                row[f"{name}_{field}"] = mtr.get(field)
            for order, group in mtr.get("groups", {}).items():
                row[f"{name}_group{order}_A2"] = group["group"]["mean_square_A2"]
                row[f"{name}_strict{order}_A2"] = group["strict"]["mean_square_A2"] if group["strict"] else None
        rows.append(row)
    fields = list(dict.fromkeys(k for row in rows for k in row)) or ["capture", "quality"]
    tmp = root / "summary.csv.tmp"
    with tmp.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(root / "summary.csv")
    return len(rows)
