"""Fake IPC worker used exclusively by test_bridge.ps1; no instrument imports."""
import argparse
from pathlib import Path
import time
from common import atomic_json


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("--config")
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    root = Path(args.directory)
    atomic_json(root / "settings.json", {"acquisition": {"record_length": 50000}})
    atomic_json(root / "status.json", {"state": "prepared"})
    for command, state in (("acquire", "frozen"), ("export", "complete")):
        deadline = time.monotonic()+10
        while not (root / (command+".json")).exists():
            if (root / "abort.json").exists() or time.monotonic() > deadline:
                raise SystemExit(1)
            time.sleep(0.01)
        if state == "complete":
            atomic_json(root / "manifest.json", {"fixture": True})
        atomic_json(root / "status.json", {"state": state})
