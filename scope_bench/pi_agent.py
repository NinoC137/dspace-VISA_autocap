"""SSH stdio endpoint on the Pi. Stdlib only; waveform bytes stream to mounted SMB.

No acquisition runs on startup or disconnect. Only explicit RPC writes touch SCPI.
Use the delivered /root/rsvisa.py bindings with bounded reads and actual I/O timeouts.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

PROTOCOL = 1


class VisaFailure(RuntimeError):
    def __init__(self, code, operation):
        self.code = code
        super().__init__(f"{operation}: VISA 0x{code & 0xffffffff:08X}")


class Visa:
    def __init__(self, resource):
        sys.path.insert(0, "/root")
        import rsvisa as binding
        self.v = binding
        self.lib = binding._lib
        self.lib.viSetAttribute.argtypes = [binding.ViSession, ctypes.c_uint32, ctypes.c_size_t]
        self.lib.viSetAttribute.restype = ctypes.c_int32
        self.rm, self.vi = binding.ViSession(), binding.ViSession()
        try:
            self.check(self.lib.viOpenDefaultRM(ctypes.byref(self.rm)), "open RM")
            self.check(self.lib.viOpen(self.rm, resource.encode("ascii"), 0, 5000,
                                      ctypes.byref(self.vi)), "open instrument")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(status, operation):
        # VISA success with more bytes pending (VI_SUCCESS_MAX_CNT) is positive.
        if status < 0:
            raise VisaFailure(status, operation)

    def timeout(self, milliseconds):
        self.check(self.lib.viSetAttribute(self.vi, 0x3FFF001A, int(milliseconds)), "set timeout")

    def write(self, command):
        data = (command + "\n").encode("ascii")
        count = self.v.ViUInt32()
        self.check(self.lib.viWrite(self.vi, data, len(data), ctypes.byref(count)), "write")
        if count.value != len(data):
            raise IOError("Incomplete VISA write")

    def read(self, count):
        buf, actual = ctypes.create_string_buffer(count), self.v.ViUInt32()
        self.check(self.lib.viRead(self.vi, buf, count, ctypes.byref(actual)), "read")
        if not actual.value:
            raise EOFError("Instrument returned an empty read")
        return buf.raw[:actual.value]

    def query(self, command):
        self.write(command)
        data = bytearray()
        while len(data) < 65536:
            data.extend(self.read(4096))
            if data.endswith(b"\n"):
                return data.decode("ascii").strip()
        raise ValueError("Text response exceeds 64 KiB")

    def clear(self):
        self.check(self.lib.viClear(self.vi), "clear")

    def close(self):
        if self.vi.value:
            self.lib.viClose(self.vi)
            self.vi.value = 0
        if self.rm.value:
            self.lib.viClose(self.rm)
            self.rm.value = 0


def stream_block(read, destination, expected_bytes, expect_termination=True, max_bytes=32*1024**2):
    """Read a strict IEEE definite-length block without buffering the waveform."""
    def exact(count):
        data = bytearray()
        while len(data) < count:
            block = read(count - len(data))
            if not block or len(block) > count-len(data):
                raise EOFError("Truncated/invalid VISA block")
            data.extend(block)
        return bytes(data)

    prefix = exact(2)
    if prefix[:1] != b"#" or prefix[1:2] not in b"123456789":
        raise ValueError("Expected definite-length IEEE block")
    length = exact(int(prefix[1:2]))
    if not length.isdigit() or (expected_bytes is not None and int(length) != expected_bytes):
        raise ValueError(f"Waveform size mismatch: expected {expected_bytes}, header {length!r}")
    if expected_bytes is None:
        expected_bytes = int(length)
        if not 1 <= expected_bytes <= max_bytes:
            raise ValueError("Screenshot block exceeds size limit")
    target = Path(destination)
    partial = target.with_suffix(target.suffix + ".partial")
    digest = hashlib.sha256()
    with partial.open("xb") as out:
        remaining = expected_bytes
        while remaining:
            block = exact(min(65536, remaining))
            out.write(block)
            digest.update(block)
            remaining -= len(block)
        if expect_termination:
            end = exact(1)
            if end == b"\r":
                end = exact(1)
            if end != b"\n":
                raise ValueError("Invalid binary block terminator")
        out.flush()
        os.fsync(out.fileno())
    partial.replace(target)
    return dict(bytes=expected_bytes, sha256=digest.hexdigest(), filename=target.name)


def check_mount(root):
    """Refuse a plain local directory after SMB disconnect/unmount."""
    root = Path(root).resolve()
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        mount = fields[1].replace("\\040", " ").replace("\\134", "\\")
        if Path(mount) == root and fields[2] == "cifs":
            return root
    raise RuntimeError(f"SMB mount missing at {root}; refusing local Pi storage")


def serve(root, resource):
    import fcntl
    key = hashlib.sha256(resource.encode()).hexdigest()[:24]
    with open(f"/tmp/mxo_bench_{key}.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session_dir, instrument = None, None
        try:
            for line in sys.stdin:
                request = json.loads(line)
                try:
                    op = request["op"]
                    if op == "hello":
                        if session_dir is not None:
                            raise RuntimeError("Session already initialized")
                        if request["protocol"] != PROTOCOL:
                            raise ValueError("Incompatible Pi agent version")
                        token = request["token"]
                        if not re.fullmatch(r"[0-9a-f]{32}", token):
                            raise ValueError("Invalid session token")
                        root = check_mount(root)
                        session_dir = root / ".transport" / token
                        session_dir.mkdir(parents=True, exist_ok=False)
                        probe = token.encode("ascii")
                        with (session_dir / "probe.txt").open("xb") as out:
                            out.write(probe)
                            out.flush()
                            os.fsync(out.fileno())
                        result = dict(protocol=PROTOCOL, sha256=hashlib.sha256(probe).hexdigest(),
                                      bytes=len(probe), free_bytes=shutil.disk_usage(root).free)
                    elif op == "close":
                        print(json.dumps(dict(ok=True, result=None)), flush=True)
                        break
                    else:
                        if session_dir is None:
                            raise RuntimeError("Send hello first")
                        if op == "storage":
                            check_mount(root)
                            result = dict(free_bytes=shutil.disk_usage(root).free)
                        else:
                            if instrument is None:
                                instrument = Visa(resource)
                            budget = int(request["timeout_ms"])
                            if not 1 <= budget <= 86400000:
                                raise ValueError("Invalid I/O timeout")
                            instrument.timeout(budget)
                            if op == "query":
                                result = instrument.query(request["command"])
                            elif op == "write":
                                result = instrument.write(request["command"])
                            elif op == "clear":
                                result = instrument.clear()
                            elif op in ("binary", "screenshot"):
                                check_mount(root)
                                screenshot = op == "screenshot"
                                expected = None if screenshot else int(request["expected_bytes"])
                                if not screenshot and (expected < 8 or expected % 4 or expected > 4*1024**3):
                                    raise ValueError("Invalid REAL32 waveform size")
                                if shutil.disk_usage(root).free < (expected or 32*1024**2) + 256*1024**2:
                                    raise OSError("Insufficient SMB disk space")
                                pattern = r"[0-9a-f]{32}\.png" if screenshot else r"[0-9a-f]{32}\.f32"
                                if not re.fullmatch(pattern, request["filename"]):
                                    raise ValueError("Invalid payload filename")
                                deadline = time.monotonic() + budget/1000
                                instrument.write("HCOPy:DATA?" if screenshot else request["command"])
                                def read(count):
                                    remaining = deadline - time.monotonic()
                                    if remaining <= 0:
                                        raise TimeoutError("Whole-block VISA transfer deadline exceeded")
                                    instrument.timeout(max(1, int(remaining*1000)))
                                    return instrument.read(count)
                                result = stream_block(read, session_dir/request["filename"], expected,
                                                      request["expect_termination"])
                            else:
                                raise ValueError("Unknown operation " + op)
                    print(json.dumps(dict(ok=True, result=result)), flush=True)
                except Exception as exc:
                    print(json.dumps(dict(ok=False, error=str(exc),
                                          visa_code=getattr(exc, "code", None))), flush=True)
        finally:
            if instrument:
                instrument.close()
            # Keep incomplete transfers for diagnosis. PC removes verified payloads.
            if session_dir:
                try:
                    (session_dir/"probe.txt").unlink(missing_ok=True)
                    session_dir.rmdir()
                except OSError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--resource", required=True)
    args = parser.parse_args()
    try:
        serve(args.root, args.resource)
    except Exception as exc:
        print(json.dumps(dict(ok=False, error=str(exc))), flush=True)
        sys.exit(1)
