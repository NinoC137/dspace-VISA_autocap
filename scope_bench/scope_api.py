"""Agent-facing CLI over the MXO38 bench. One JSON object per invocation on stdout.

This is the single entry point an AI agent (or the MCP wrapper) uses to observe and
capture waveforms. It never touches the converter: ControlDesk parameters, Enable1
and the recorders are owned by run_parameter_sweep.ps1, not by this tool.

Safety contract: the operator de-energizes the device under test before a capture.
`capture` refuses to run when an interlock command is configured and fails.

Every subcommand prints exactly one JSON object to stdout and exits 0 on success,
1 on a handled failure. Diagnostics go to stderr so stdout stays parseable.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import atomic_json, load_config, read_json, selected_channels, validate  # noqa: E402

DEFAULT_CONFIG = HERE / "bench_config.json"


def emit(payload, code=0):
    json.dump(payload, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return code


def fail(message, **extra):
    payload = dict(ok=False, error=str(message))
    payload.update(extra)
    return emit(payload, 1)


def load(path):
    return load_config(Path(path).resolve())


def read_state(scope):
    """Read-only instrument state. Never writes to the instrument."""
    info = scope.snapshot()
    info["idn_detailed"] = scope.identity()
    return info


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def new_capture_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}"


def config_with_overrides(config, args):
    """Apply per-invocation capture overrides without mutating the saved profile."""
    if args.record_duration_s is not None:
        config["timing"]["record_duration_s"] = float(args.record_duration_s)
    if args.sample_rate_hz is not None:
        value = float(args.sample_rate_hz)
        config["acquisition"]["sample_rate_hz"] = None if value <= 0 else value
    if args.export_timeout_s is not None:
        config["instrument"]["export_timeout_s"] = float(args.export_timeout_s)
    if args.analysis_channel is not None:
        config["analysis"]["channel"] = args.analysis_channel
    if args.channels is not None:
        wanted = {name.strip().upper() for name in args.channels.split(",") if name.strip()}
        unknown = wanted - {f"CH{i}" for i in range(1, 9)}
        if unknown:
            raise ValueError("Unknown channel(s): " + ", ".join(sorted(unknown)))
        if not wanted:
            raise ValueError("At least one channel must be enabled.")
        for name, item in config["channels"].items():
            item["enabled"] = name in wanted
    if args.unit is not None:
        unit = args.unit.strip().upper()
        if unit not in ("A", "V", "W"):
            raise ValueError("Unit must be A, V or W.")
        for name in selected_channels(config):
            config["channels"][name]["unit"] = unit
            if config["channels"][name]["signal"] in (None, "", name):
                config["channels"][name]["signal"] = name
    return validate(config)


def cmd_ping(args):
    config = load(args.config)
    from mxo import Mxo
    scope = Mxo(config)
    try:
        scope.errors()
        return emit(dict(ok=True, idn=scope.identity(), resource=config["instrument"]["resource"],
                         connection=getattr(scope.session, "connection_info", {"transport": "local_visa"}),
                         config_path=str(Path(args.config).resolve())))
    finally:
        scope.close()


def cmd_state(args):
    config = load(args.config)
    from mxo import Mxo
    scope = Mxo(config)
    try:
        return emit(dict(ok=True, state=read_state(scope)))
    finally:
        scope.close()


def cmd_raw(args):
    """Query-only escape hatch. Writes are refused here on purpose."""
    command = args.command.strip()
    if not command.endswith("?") and not command.startswith("*"):
        return fail("Only queries are allowed here; refusing a write: " + command)
    if "*RST" in command.upper() or "*CLS" in command.upper():
        return fail("That command changes instrument state; refusing: " + command)
    config = load(args.config)
    from mxo import Mxo
    scope = Mxo(config)
    try:
        return emit(dict(ok=True, command=command, response=scope.query(command)))
    finally:
        scope.close()


def cmd_captures(args):
    root = Path(args.directory).resolve()
    items = []
    if root.is_dir():
        for manifest in sorted(root.rglob("manifest.json")):
            try:
                item = read_json(manifest)
            except Exception as exc:
                items.append(dict(directory=str(manifest.parent), error=str(exc)))
                continue
            metrics = item.get("metrics")
            items.append(dict(
                capture=manifest.parent.name, directory=str(manifest.parent),
                capture_status=item.get("capture_status"), analysis_status=item.get("analysis_status"),
                channels=item.get("channels"), file=item.get("file"), sha256=item.get("sha256"),
                metrics=metrics, error=item.get("error")))
    return emit(dict(ok=True, count=len(items), captures=items[-args.limit:]))


def cmd_capture(args):
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.interlock:
        check = subprocess.run(args.interlock, shell=True, capture_output=True, text=True)
        if check.returncode != 0:
            return fail("Interlock refused the capture.", interlock=args.interlock,
                        stdout=check.stdout.strip()[-2000:], stderr=check.stderr.strip()[-2000:])
    capture_dir = directory / new_capture_id()
    config = config_with_overrides(load(args.config), args)
    if args.channels is not None:
        for name in selected_channels(config):
            if config["channels"][name]["unit"] is None:
                return fail(f"{name} has no measurement unit configured; it cannot be captured as a "
                            "raw channel. Add a unit in bench_config.json first.")
    atomic_json(capture_dir / "config.json", config)
    atomic_json(capture_dir / "context.json", dict(
        Case=dict(Id=args.label or "manual"), Readback={}, Source="scope_api",
        Note="Captured by scope_api.py; no ControlDesk parameter was written."))
    return emit(dict(ok=True, started=True, capture=capture_dir.name, directory=str(capture_dir),
                     channels=selected_channels(config),
                     record_duration_s=config["timing"]["record_duration_s"],
                     sample_rate_hz=config["acquisition"]["sample_rate_hz"],
                     note="Capture runs in a child process. Poll scope_capture_status."))


def cmd_capture_worker(args):
    """Internal: run the bench worker in the foreground."""
    directory = Path(args.directory).resolve()
    if args.worker:
        result = subprocess.run([sys.executable, "-u", str(args.worker), str(directory)])
        return result.returncode
    config = read_json(directory / "config.json")
    from bench import worker
    return worker(config, directory, standalone=True)


def capture_status(directory):
    status_path = directory / "status.json"
    payload = dict(ok=True, directory=str(directory), state="starting")
    if status_path.exists():
        try:
            payload["status"] = read_json(status_path)
            payload["state"] = payload["status"].get("state", "starting")
        except Exception as exc:
            payload["state"] = "unknown"
            payload["status_error"] = str(exc)
    manifest = directory / "manifest.json"
    if manifest.exists():
        try:
            payload["manifest"] = read_json(manifest)
            if payload["manifest"].get("analysis_status") == "skipped":
                payload["note"] = ("Waveform captured; spectral analysis skipped: "
                                   + str(payload["manifest"].get("analysis_skipped_reason", "")))
        except Exception as exc:
            payload["manifest_error"] = str(exc)
    error = directory / "error.txt"
    if error.exists():
        payload["error_tail"] = error.read_text(encoding="utf-8", errors="replace")[-2000:]
    return payload


def cmd_capture_status(args):
    return emit(capture_status(Path(args.directory).resolve()))


def cmd_capture_abort(args):
    directory = Path(args.directory).resolve()
    if not directory.is_dir():
        return fail("No such capture directory: " + str(directory))
    workdir = Path(args.workdir).resolve()
    pid_file = workdir / "worker.pid"
    started = []
    marker = directory / "abort.json"
    if not marker.exists():
        marker.write_text("{}", encoding="utf-8")
        started.append("abort marker")
    # The worker consumes this marker between operations. Do not kill a waveform
    # transfer while the Pi is committing its sole acquired record to the share.
    return emit(dict(ok=True, directory=str(directory), actions=started,
                     state=capture_status(directory).get("state")))


def cmd_capture_start(args):
    """Spawn a detached worker and return immediately, so no SSH session is held open."""
    directory = Path(args.directory).resolve()
    workdir = Path(args.workdir).resolve()
    if (directory / "status.json").exists():
        return fail("Capture directory already used; create a new capture.")
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = directory / "worker.log"
    command = [sys.executable, "-u", str(HERE / "scope_api.py"), "capture-worker",
               "--directory", str(directory)]
    if args.worker:
        command.extend(["--worker", str(args.worker)])
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command,
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                          getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    (workdir / "worker.pid").write_text(str(process.pid), encoding="utf-8")
    return emit(dict(ok=True, started=True, pid=process.pid, directory=str(directory),
                     log=str(log_path)))


def main():
    parser = argparse.ArgumentParser(description="MXO38 bench API for agents.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="action", required=True)

    sub.add_parser("ping", help="Verify the scope answers SCPI.")

    sub.add_parser("state", help="Read-only instrument state.")

    raw = sub.add_parser("raw", help="Run one read-only SCPI query.")
    raw.add_argument("--command", required=True)

    captures = sub.add_parser("captures", help="List past captures with metrics.")
    captures.add_argument("--directory", required=True)
    captures.add_argument("--limit", type=int, default=20)

    capture = sub.add_parser("capture", help="Prepare a capture directory.")
    capture.add_argument("--directory", required=True)
    capture.add_argument("--channels", default=None, help="Comma separated, e.g. CH2")
    capture.add_argument("--unit", default=None,
                         help="Measurement unit for the captured channels: A, V or W.")
    capture.add_argument("--record-duration-s", type=float, default=None)
    capture.add_argument("--sample-rate-hz", type=float, default=None)
    capture.add_argument("--export-timeout-s", type=float, default=None)
    capture.add_argument("--analysis-channel", default=None)
    capture.add_argument("--label", default=None)
    capture.add_argument("--interlock", default=None,
                         help="Shell command that must exit 0 before any capture.")

    start = sub.add_parser("capture-start", help="Start a prepared capture in the background.")
    start.add_argument("--directory", required=True)
    start.add_argument("--workdir", required=True)
    start.add_argument("--worker", default=None,
                       help="Worker entry point; defaults to bench.py (test seam only).")

    worker = sub.add_parser("capture-worker", help="Internal: run the bench worker.")
    worker.add_argument("--directory", required=True)
    worker.add_argument("--worker", default=None,
                        help="Worker entry point; defaults to bench.py (test seam only).")

    status = sub.add_parser("capture-status", help="Report a capture's state.")
    status.add_argument("--directory", required=True)

    abort = sub.add_parser("capture-abort", help="Abort a capture.")
    abort.add_argument("--directory", required=True)
    abort.add_argument("--workdir", required=True)

    args = parser.parse_args()
    handlers = {
        "ping": cmd_ping, "state": cmd_state, "raw": cmd_raw, "captures": cmd_captures,
        "capture": cmd_capture, "capture-start": cmd_capture_start,
        "capture-worker": cmd_capture_worker, "capture-status": cmd_capture_status,
        "capture-abort": cmd_capture_abort,
    }
    try:
        return handlers[args.action](args)
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
