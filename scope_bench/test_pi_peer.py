"""Subprocess fixture for SSH/SMB protocol tests. Never imports a hardware driver."""
import io
from pathlib import Path
import sys
import types

import numpy as np
import pi_agent
from common import load_config
from test_screenshots import DisplayVisa, png_bytes


if __name__ == "__main__":
    cfg = load_config(sys.argv[2])
    class Instrument(DisplayVisa):
        def __init__(self, resource):
            super().__init__(cfg)
        def timeout(self, milliseconds):
            pass
        def query(self, command):
            if command == "TEST:DISCONNECT?":
                sys.exit(2)
            if command == "TEST:TIMEOUT?":
                import time
                time.sleep(5)
            return super().query(command)
        def write(self, command):
            if command.endswith(":DATA:VALues?") or command == "HCOPy:DATA?":
                y = png_bytes() if command == "HCOPy:DATA?" else self.query_binary_values(command).astype("<f4").tobytes()
                size = str(len(y)).encode()
                self.payload = io.BytesIO(b"#"+str(len(size)).encode()+size+y+b"\n")
            else:
                super().write(command)
        def read(self, count):
            return self.payload.read(min(count, 713))
    pi_agent.Visa = Instrument
    pi_agent.check_mount = lambda root: Path(root)
    if sys.platform == "win32":
        sys.modules["fcntl"] = types.SimpleNamespace(flock=lambda *_: None, LOCK_EX=1, LOCK_NB=2)
        # serve uses /tmp only for its advisory lock, not waveform data.
        original_open = open
        def fixture_open(path, *args, **kwargs):
            if str(path).startswith("/tmp/mxo_bench_"):
                path = Path(sys.argv[1])/Path(path).name
            return original_open(path, *args, **kwargs)
        pi_agent.open = fixture_open
    pi_agent.serve(sys.argv[1], "fixture")
