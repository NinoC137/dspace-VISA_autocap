"""CLI and per-case worker. File messages are private to a unique capture directory."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import traceback

from common import atomic_json, load_config, read_json, selected_channels


def worker(config, directory, scope_factory=None, standalone=False):
    from mxo import Mxo
    from analysis import analyze_file
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "status.json").exists():
        raise FileExistsError("Capture directory already used.")
    scope = None
    state = "starting"

    def status(value, **extra):
        nonlocal state
        state = value
        item = dict(state=state, utc=datetime.now(timezone.utc).isoformat(), **extra)
        atomic_json(directory / "status.json", item)
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def wait_command(name):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if (directory / "abort.json").exists():
                raise RuntimeError("Stop requested by sweep controller.")
            if standalone:
                return
            if (directory / (name + ".json")).exists():
                return
            time.sleep(0.02)
        raise TimeoutError("Controller did not send " + name)

    try:
        status("starting")
        atomic_json(directory / "config.json", config)
        scope = (scope_factory or Mxo)(config)
        scope.prepare(directory)
        status("prepared")
        wait_command("acquire")
        status("acquiring")
        scope.acquire()
        status("frozen")
        wait_command("export")
        status("exporting")
        raw = scope.export(directory, read_json(directory / "context.json"))
        from screenshots import export_screenshots
        status("screenshots")
        shots = export_screenshots(scope, directory, read_json(directory / "context.json"), raw)
        manifest = read_json(directory / "manifest.json")
        manifest["screenshots"] = shots
        manifest["link"] = scope.link_report()
        atomic_json(directory / "manifest.json", manifest)
        warnings = []
        if shots["status"] == "failed":
            warnings.append("截图未完整保存："+shots.get("error", "; ".join(shots["restore_errors"])))
            print("WARNING: "+warnings[0], flush=True)
        status("analyzing")
        # Spectral analysis is defined for a current channel. A scope-only capture
        # (a channel with no configured unit) still produces waveform metadata.
        analysis_channel = config["analysis"]["channel"]
        if config["channels"][analysis_channel]["unit"] is None:
            manifest = read_json(directory / "manifest.json")
            manifest.update(analysis_status="skipped",
                            analysis_skipped_reason=f"{analysis_channel} has no configured unit")
            atomic_json(directory / "manifest.json", manifest)
            status("complete", analysis="skipped", warnings=warnings)
            return 0
        metrics = analyze_file(raw)
        manifest = read_json(directory / "manifest.json")
        manifest.update(analysis_status="complete", metrics=str(metrics.relative_to(directory)))
        atomic_json(directory / "manifest.json", manifest)
        status("complete", metrics=manifest["metrics"], warnings=warnings)
        return 0
    except BaseException as exc:
        (directory / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            manifest.update(analysis_status="failed", error=str(exc))
            atomic_json(manifest_path, manifest)
        status("failed", error=str(exc))
        return 1
    finally:
        if scope:
            try:
                scope.close(stop=state != "complete")
            except Exception as exc:
                status("failed", error="Scope cleanup: " + str(exc))
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "check", "worker", "analyze", "summarize"])
    parser.add_argument("--config", default=str(Path(__file__).with_name("bench_config.json")))
    parser.add_argument("--directory")
    parser.add_argument("--file")
    args = parser.parse_args()
    if args.action == "analyze":
        from analysis import analyze_file
        if not args.file:
            parser.error("analyze needs --file")
        print(analyze_file(args.file), flush=True)
        return 0
    if args.action == "summarize":
        from analysis import summarize
        if not args.directory:
            parser.error("summarize needs --directory")
        print("Summary rows:", summarize(args.directory), flush=True)
        return 0
    config = load_config(args.config)
    if args.action == "plan":
        print(json.dumps(dict(transport=config["instrument"].get("transport", "local_visa"),
                              channels=selected_channels(config), timing=config["timing"],
                              acquisition=config["acquisition"]), indent=2), flush=True)
    elif args.action == "check":
        from mxo import Mxo
        scope = Mxo(config)
        try:
            print(json.dumps(scope.snapshot(), indent=2), flush=True)
        finally:
            scope.close()
    else:
        if not args.directory:
            parser.error("worker needs --directory")
        return worker(config, args.directory)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
