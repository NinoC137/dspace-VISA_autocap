#!/usr/bin/env python3
"""Run every existing bench test suite and report a one-line summary each."""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

SUITES = ["test_bench.py", "test_config_migration.py", "test_screenshots.py",
          "test_switch_scope.py", "test_tek_backend.py", "test_api.py",
          "test_pi_transport.py"]

failed = []
for name in SUITES:
    path = HERE / name
    if not path.exists():
        print("  %-30s SKIP (missing)" % name)
        continue
    done = subprocess.run([PY, str(path)], capture_output=True, text=True,
                          cwd=str(HERE), timeout=600)
    text = (done.stdout or "") + (done.stderr or "")
    ran = re.search(r"Ran (\d+) tests?", text)
    verdict = "OK" if re.search(r"^OK", text, re.M) else "FAILED"
    errors = len(re.findall(r"^(FAIL|ERROR):", text, re.M))
    print("  %-30s %-8s %s tests%s" % (
        name, verdict, ran.group(1) if ran else "?",
        "" if verdict == "OK" else "  (%d problems)" % errors))
    if verdict != "OK":
        failed.append(name)
        for line in text.splitlines():
            if line.startswith(("FAIL:", "ERROR:")):
                print("        %s" % line.strip())
print()
print("ALL SUITES PASS" if not failed else "FAILED SUITES: %s" % ", ".join(failed))
sys.exit(1 if failed else 0)
