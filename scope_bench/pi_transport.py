"""PC-side VISA-shaped session: SSH control, Pi USB, SMB waveform delivery."""
import hashlib
import json
from pathlib import Path, PureWindowsPath
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid

import numpy as np
from pyvisa.errors import VisaIOError

from common import sha256


def validate_remote(remote):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*", remote["host"]):
        raise ValueError("remote.host must be user@host")
    for key in ("python", "agent", "share_root"):
        value = remote[key]
        if not value.startswith("/") or any(c in value for c in "\r\n\0"):
            raise ValueError(f"remote.{key} must be an absolute Pi path")
    if not (Path(remote["local_share_root"]).is_absolute() or
            PureWindowsPath(remote["local_share_root"]).is_absolute()):
        raise ValueError("remote.local_share_root must be an absolute PC path")


def verify_delivery(path, metadata, timeout_s=10):
    """SMB can lag; verify content, not just presence or apparent file size."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if path.stat().st_size == metadata["bytes"] and sha256(path) == metadata["sha256"]:
                return
        except (FileNotFoundError, PermissionError):
            pass
        if time.monotonic() >= deadline:
            raise IOError(f"SMB delivery missing or checksum mismatch: {path}")
        time.sleep(0.1)


class PiSession:
    def __init__(self, config):
        self.remote = config["instrument"]["remote"]
        validate_remote(self.remote)
        self.timeout = config["instrument"]["query_timeout_ms"]
        self.expected_bytes = None
        self.closed = False
        self.replies = queue.Queue()
        self.errors = []
        self.token = uuid.uuid4().hex
        self.folder = Path(self.remote["local_share_root"]) / ".transport" / self.token
        command = shlex.join([self.remote["python"], "-u", self.remote["agent"],
                              "--root", self.remote["share_root"],
                              "--resource", config["instrument"]["resource"]])
        self.process = subprocess.Popen(
            [self.remote.get("ssh_executable", "ssh"), "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
             self.remote["host"], command],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def receive():
            try:
                for line in self.process.stdout:
                    self.replies.put(line)
            finally:
                self.replies.put(None)
        def errors():
            for line in self.process.stderr:
                self.errors.append(line.rstrip())
                self.errors[:] = self.errors[-12:]
        threading.Thread(target=receive, daemon=True).start()
        threading.Thread(target=errors, daemon=True).start()
        try:
            hello = self.rpc("hello", protocol=1, token=self.token, wait_s=15)
            if hello["protocol"] != 1 or hello["sha256"] != hashlib.sha256(self.token.encode()).hexdigest():
                raise RuntimeError("Pi agent handshake failed")
            verify_delivery(self.folder/"probe.txt", hello)
            self.connection_info = dict(transport="raspi_ssh", host=self.remote["host"],
                pi_share=self.remote["share_root"], pc_share=self.remote["local_share_root"],
                storage_verified=True, protocol=1)
        except BaseException:
            self.close()
            raise

    def rpc(self, op, wait_s=None, **values):
        if self.closed:
            raise ConnectionError("Pi SSH session is closed; reconnect before retrying")
        try:
            self.process.stdin.write(json.dumps(dict(op=op, timeout_ms=self.timeout, **values))+"\n")
            self.process.stdin.flush()
            line = self.replies.get(timeout=wait_s if wait_s is not None else self.timeout/1000+2)
            if line is None:
                raise ConnectionError("Pi SSH connection ended: " + "; ".join(self.errors))
            response = json.loads(line)
        except queue.Empty:
            self.terminate()
            raise TimeoutError(f"Pi SSH {op} timed out; session invalidated") from None
        except (OSError, ValueError):
            self.terminate()
            raise
        if not response["ok"]:
            if response.get("visa_code") is not None:
                raise VisaIOError(response["visa_code"])
            raise RuntimeError("Pi: " + response["error"])
        return response["result"]

    def query(self, command):
        return self.rpc("query", command=command)

    def write(self, command):
        return self.rpc("write", command=command)

    def clear(self):
        return self.rpc("clear")

    def check_storage(self, bytes_required):
        result = self.rpc("storage")
        if result["free_bytes"] < bytes_required:
            raise OSError("Insufficient free space on the Pi's PC network drive")

    def query_binary_values(self, command, **kwargs):
        if kwargs.get("datatype") != "f" or kwargs.get("is_big_endian"):
            raise ValueError("Pi transport requires little-endian REAL32")
        if not self.expected_bytes:
            raise ValueError("Waveform length must be known before transfer")
        filename = uuid.uuid4().hex + ".f32"
        result = self.rpc("binary", command=command, filename=filename,
                          expected_bytes=self.expected_bytes,
                          expect_termination=kwargs.get("expect_termination", True))
        if result["filename"] != filename or result["bytes"] != self.expected_bytes:
            raise ValueError("Pi returned unexpected payload metadata")
        path = self.folder/filename
        verify_delivery(path, result)
        # PC owns analysis memory; the 32-bit Pi keeps only a 64 KiB transfer buffer.
        values = np.fromfile(path, dtype="<f4")
        if values.nbytes != self.expected_bytes:
            raise ValueError("Incomplete local waveform read")
        path.unlink()
        return values

    def terminate(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)

    def screenshot_bytes(self):
        filename = uuid.uuid4().hex + ".png"
        result = self.rpc("screenshot", filename=filename, expect_termination=True)
        if result["filename"] != filename or not 1 <= result["bytes"] <= 32*1024**2:
            raise ValueError("Pi returned unexpected screenshot metadata")
        path = self.folder / filename
        verify_delivery(path, result)
        data = path.read_bytes()
        path.unlink()
        return data

    def close(self):
        if not self.closed and self.process.poll() is None:
            try:
                self.rpc("close", wait_s=2)
                self.process.stdin.close()
                self.process.wait(timeout=3)
            except Exception:
                self.terminate()
        if self.process.poll() is None:
            self.terminate()
        self.closed = True
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
