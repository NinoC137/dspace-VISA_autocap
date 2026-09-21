"""Configuration and atomic local IPC. No instrument imports here."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
import re


DEFAULT_SCREENSHOT_SPANS = [0.2, 0.05, 0.01, 0.001, 0.0001]


def parse_spans(text):
    """Operator input is a list of total screen spans, never seconds per division."""
    result = []
    for item in re.split(r"[,，;；]+", text):
        match = re.fullmatch(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*(s|ms|us|µs|μs)\s*", item, re.I)
        if not match:
            raise ValueError("截图跨度请写成 200ms, 50ms, 10ms, 1ms, 100us")
        result.append(float(match[1])/{"s": 1, "ms": 1e3, "us": 1e6, "µs": 1e6, "μs": 1e6}[match[2].lower()])
    return result


def time_label(seconds):
    for unit, factor in (("s", 1), ("ms", 1e-3), ("us", 1e-6)):
        if seconds >= factor*(1-1e-10):
            return f"{seconds/factor:g}{unit}"
    return f"{seconds/1e-6:g}us"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    # A Windows reader may briefly hold the destination open.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def selected_channels(config):
    return [name for name, channel in config["channels"].items() if channel["enabled"]]


def validate(config):
    if config.get("schema_version") != 1 or "status" in config:
        raise ValueError("Use bench_config.json, not the old design-contract example.")
    channels = config["channels"]
    if set(channels) != {f"CH{i}" for i in range(1, 9)}:
        raise ValueError("channels must contain CH1 through CH8 exactly.")
    for name, channel in channels.items():
        if type(channel["enabled"]) is not bool:
            raise ValueError(f"{name}.enabled must be true or false.")
        if channel["unit"] not in (None, "A", "V", "W"):
            raise ValueError(f"Unsupported unit for {name}.")
        if not math.isfinite(float(channel["zero_offset"])):
            raise ValueError("Offsets must be finite.")
    if not selected_channels(config):
        raise ValueError("Enable at least one channel.")
    t, ins, a = config["timing"], config["instrument"], config["analysis"]
    shots = config.setdefault("screenshots", {})
    for key, value in dict(enabled=True, spans_s=DEFAULT_SCREENSHOT_SPANS.copy(),
                           center_fraction=0.5, settle_s=0.5, timeout_s=30).items():
        shots.setdefault(key, value)
    if type(shots["enabled"]) is not bool:
        raise ValueError("screenshots.enabled must be true or false")
    spans = shots["spans_s"]
    if (not isinstance(spans, list) or not 1 <= len(spans) <= 12 or
            any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in spans) or
            len(set(spans)) != len(spans)):
        raise ValueError("Choose 1-12 distinct positive screenshot spans")
    for key, low, high in (("center_fraction", 0, 1), ("settle_s", 0.1, 5), ("timeout_s", 5, 120)):
        value = shots[key]
        if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid screenshots.{key}")
    # Old GUI versions saved schema 1 without the subsequently added link fields.
    for key, default in {"min_throughput_bytes_per_s": 200000,
                         "settle_after_clear_s": 0.6, "query_attempts": 2,
                         "recover_attempts": 2}.items():
        ins.setdefault(key, default)
    if ins.get("transport", "local_visa") not in ("local_visa", "raspi_ssh"):
        raise ValueError("instrument.transport must be local_visa or raspi_ssh")
    if ins.get("transport") == "raspi_ssh":
        from pi_transport import validate_remote
        validate_remote(ins["remote"])
    for obj, keys in ((t, list(t)), (ins, ["query_timeout_ms", "acquisition_timeout_s", "export_timeout_s"]),
                      (a, ["fsw_hz", "f0_hz", "welch_segment_s", "dc_span_abs", "dc_span_relative", "ac_span_relative"])):
        if any(not math.isfinite(float(obj[k])) or float(obj[k]) <= 0 for k in keys):
            raise ValueError("Timing, frequencies and thresholds must be finite and positive.")
    for key in ("min_throughput_bytes_per_s", "settle_after_clear_s",
                "query_attempts", "recover_attempts"):
        if not math.isfinite(float(ins[key])) or float(ins[key]) <= 0:
            raise ValueError(f"instrument.{key} must be finite and positive.")
    if not 10 <= t["enable_duration_s"] <= 15 or t["minimum_off_s"] < 1:
        raise ValueError("Use 10-15 s enable and at least 1 s off.")
    if t["settle_s"] + ins["acquisition_timeout_s"] + 1 >= t["enable_duration_s"]:
        raise ValueError("Insufficient shutdown margin after acquisition.")
    if not 2 * a["welch_segment_s"] <= t["record_duration_s"] < ins["acquisition_timeout_s"]:
        raise ValueError("Record must cover two Welch windows and fit acquisition timeout.")
    if shots["enabled"]:
        half = max(spans)/2
        center = shots["center_fraction"]*t["record_duration_s"]
        if half > min(center, t["record_duration_s"]-center)+1e-12:
            raise ValueError("截图范围超出采集记录；请缩短截图跨度或将截图中心移回记录中央。")
    if type(a["q_max"]) is not int or a["q_max"] < 0 or not 0 <= a["overlap"] < 1:
        raise ValueError("Invalid q_max or Welch overlap.")
    if 2 / a["welch_segment_s"] >= a["f0_hz"] / 2:
        raise ValueError("Welch window too short to separate adjacent sidebands.")
    if (a["q_max"] + 0.5) * a["f0_hz"] >= a["fsw_hz"] / 2:
        raise ValueError("Carrier groups overlap.")
    if a["channel"] not in channels:
        raise ValueError("Invalid analysis channel.")
    rate = config["acquisition"]["sample_rate_hz"]
    if rate is not None and (not math.isfinite(rate) or rate <= 0):
        raise ValueError("Invalid requested sample rate.")
    if config["acquisition"]["max_raw_bytes"] <= 0 or config["acquisition"]["max_samples_per_channel"] < 2:
        raise ValueError("Invalid storage budget.")
    return config


def load_config(path):
    config = read_json(path)
    # Keep the installed connection separately from operator choices. Legacy GUI
    # saves can replace bench_config.json but cannot erase this installation file.
    installed = Path(__file__).with_name("connection.json")
    if "transport" not in config.get("instrument", {}) and installed.exists():
        config["instrument"].update(read_json(installed))
    return validate(config)
