#!/usr/bin/env python3
"""End-to-end bench capture on the Tektronix MSO58B.

Drives the real `bench.py worker` exactly like the sweep controller does:
worker -> prepare -> (controller writes acquire.json) -> acquire
       -> (controller writes export.json) -> export + screenshots + analysis.
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

BENCH = Path(r"F:\ZYS_HYBASE\automation\scope_bench")
PY = BENCH / ".venv" / "Scripts" / "python.exe"
CFG = BENCH / "bench_config.json"
ROOT = Path(r"F:\ZYS_HYBASE\automation\scope_results")
OUT = ROOT / "tek_bench_e2e"

if OUT.exists():
    shutil.rmtree(OUT)
OUT.mkdir(parents=True)
(OUT / "context.json").write_text(json.dumps(
    {"case_id": "tek_e2e", "note": "end-to-end capture on MSO58B via tek_socket"}),
    encoding="utf-8")

print("capture dir : %s" % OUT)
print("config      : %s" % CFG)
proc = subprocess.Popen(
    [str(PY), str(BENCH / "bench.py"), "worker", "--config", str(CFG),
     "--directory", str(OUT)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

sent = set()
deadline = time.time() + 300
last = None
while time.time() < deadline:
    status_path = OUT / "status.json"
    if status_path.exists():
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))["state"]
        except Exception:
            state = None
        if state != last:
            print("  [%6.1fs] status=%s" % (time.time() - (deadline - 300), state), flush=True)
            last = state
        if state == "prepared" and "acquire" not in sent:
            (OUT / "acquire.json").write_text("{}", encoding="utf-8")
            sent.add("acquire")
        if state == "frozen" and "export" not in sent:
            (OUT / "export.json").write_text("{}", encoding="utf-8")
            sent.add("export")
        if state in ("complete", "failed"):
            break
    if proc.poll() is not None:
        break
    time.sleep(0.2)

try:
    text, _ = proc.communicate(timeout=120)
except subprocess.TimeoutExpired:
    proc.kill()
    text, _ = proc.communicate()
print("\n--- worker stdout ---")
print((text or "").strip()[:2000])
print("--- rc = %s ---" % proc.returncode)

print("\n--- produced files ---")
for item in sorted(OUT.rglob("*")):
    if item.is_file():
        print("  %-52s %10d B" % (str(item.relative_to(OUT)), item.stat().st_size))

for name in ("error.txt", "manifest.json"):
    p = OUT / name
    if p.exists():
        print("\n--- %s ---" % name)
        print(p.read_text(encoding="utf-8", errors="replace")[:1500])

metrics = sorted((OUT / "analysis").rglob("metrics.json")) if (OUT / "analysis").exists() else []
if metrics:
    report = json.loads(metrics[-1].read_text(encoding="utf-8"))
    print("\n--- analysis ---")
    print("  status          :", report.get("analysis_status"))
    print("  quality         :", report.get("quality_status"))
    print("  warnings        :", report.get("warnings"))
    for channel, values in report.get("channels", {}).items():
        keep = {k: values.get(k) for k in
                ("unit", "dc", "rms", "rms_ac", "minimum", "maximum",
                 "peak_to_peak", "two_group_rms_A", "samples", "sample_rate_hz",
                 "stationary")}
        print("  %s: %s" % (channel, json.dumps(keep, ensure_ascii=False)))
else:
    print("\n!! no metrics.json produced")
sys.exit(1 if proc.returncode else 0)
