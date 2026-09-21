"""Read-only scope link check for the bench operator.

Answers two questions without changing anything on the instrument:
  1. does the MXO38 answer SCPI at all (through either installed VISA), and
  2. how reliable is the link over repeated queries.

Run this before a batch when a sweep has failed with VI_ERROR_TMO. Queries only:
no STOP, RUN, RUNSingle, no settings writes.
"""
import glob
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import pyvisa
from common import load_config

CONFIG = str(__import__("pathlib").Path(__file__).resolve().parent / "bench_config.json")
ROUNDS = 60


def probe(label, library, resource):
    print(f"\n== {label} ==", flush=True)
    try:
        manager = pyvisa.ResourceManager(library)
    except Exception as exc:
        print(f"  cannot load {library}: {type(exc).__name__}", flush=True)
        return
    try:
        resources = []
        try:
            resources = list(manager.list_resources())
        except Exception:
            pass
        print(f"  resources: {resources or '(none)'}", flush=True)
        if not resources:
            print("  NOT ENUMERATED: check the USB cable and the scope's rear port.", flush=True)
            return
        session = manager.open_resource(resource)
        session.timeout = 5000
        session.write_termination = "\n"
        session.read_termination = "\n"
        try:
            start = time.perf_counter()
            identity = session.query("*IDN?").strip()
            print(f"  *IDN? OK in {time.perf_counter()-start:.4f} s -> {identity}", flush=True)
        except Exception as exc:
            print(f"  *IDN? FAILED: {type(exc).__name__}: {str(exc)[:60]}", flush=True)
            print("  The instrument is not answering at all. Power-cycle the MXO38,", flush=True)
            print("  then run this check again before starting a batch.", flush=True)
            return
        session.timeout = 3000
        ok = 0
        for _ in range(ROUNDS):
            try:
                session.query("*IDN?")
                ok += 1
            except Exception:
                pass
            time.sleep(0.02)
        print(f"  reliability: {ok}/{ROUNDS} queries answered ({100*ok/ROUNDS:.0f}%)", flush=True)
        for command in ("CHANnel2:STATe?", "ACQuire:RLReal?", "CHANnel2:DATA:HEADer?"):
            try:
                session.timeout = 10000
                start = time.perf_counter()
                value = session.query(command).strip()
                print(f"  {command} OK in {time.perf_counter()-start:.3f} s -> {value[:60]}", flush=True)
            except Exception as exc:
                print(f"  {command} FAILED: {type(exc).__name__}", flush=True)
                break
        session.close()
    finally:
        manager.close()


def main():
    config = load_config(CONFIG)
    resource = config["instrument"]["resource"]
    print(f"target: {resource}", flush=True)
    candidates = [("NI-VISA (default)", "@ivi")]
    for path in sorted(set(
            glob.glob(r"C:\Windows\System32\RsVisa32.dll") +
            glob.glob(r"C:\Program Files (x86)\Rohde-Schwarz\**\RsVisa32.dll", recursive=True))):
        candidates.append(("R&S VISA", path))
    for label, library in candidates:
        probe(label, library, resource)


if __name__ == "__main__":
    main()
