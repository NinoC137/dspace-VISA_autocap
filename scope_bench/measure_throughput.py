"""Read-only: measure real USB transfer throughput on the frozen record.

Queries and reads only; no acquisition, no settings writes. Used to size the transfer
timeout so it reflects the instrument instead of an assumption.
"""
import sys
import time

sys.path.insert(0, r"F:\ZYS_HYBASE\automation\scope_bench")
from mxo import Mxo, parse_header
from common import load_config

CONFIG = r"F:\ZYS_HYBASE\automation\scope_bench\bench_config.json"
LOG = r"F:\ZYS_HYBASE\automation\scope_results\_bench\throughput.log"


def log(message):
    line = f"{time.strftime('%H:%M:%S')} {message}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def read_block(session, chunk, expect_termination, budget_s):
    """Read one complete definite-length block into memory, reporting progress."""
    session.timeout = 20000
    head = session.read_bytes(2)
    if head[:1] != b"#":
        raise RuntimeError(f"not a definite-length block: {head!r}")
    digits = int(head[1:2])
    count = int(session.read_bytes(digits)) if digits else 0
    received = 0
    start = time.perf_counter()
    last = start
    while received < count:
        session.timeout = max(20000, round(1000 * budget_s))
        piece = session.read_bytes(min(chunk, count - received))
        received += len(piece)
        now = time.perf_counter()
        if now - last >= 3.0:
            log(f"    {received/1e6:7.2f}/{count/1e6:.2f} MB  {now-start:6.1f} s  "
                f"{received/(now-start)/1e6:5.2f} MB/s")
            last = now
        if now - start > budget_s:
            raise TimeoutError(f"budget {budget_s} s exceeded at {received/1e6:.2f} MB")
    elapsed = time.perf_counter() - start
    log(f"    complete: {received/1e6:.3f} MB in {elapsed:.3f} s "
        f"({received/elapsed/1e6:.3f} MB/s)")
    # Leave nothing queued: the block may be followed by a terminator.
    session.timeout = 500
    try:
        tail = session.read_bytes(1)
        log(f"    trailing byte present: {tail!r}")
    except Exception:
        pass
    return received, elapsed


def main():
    open(LOG, "w").close()
    config = load_config(CONFIG)
    scope = Mxo(config)
    session = scope.session
    try:
        for channel in (1, 2):
            try:
                raw = scope.query(f"CHANnel{channel}:DATA:HEADer?")
            except Exception as exc:
                log(f"CH{channel}: header unavailable ({type(exc).__name__})")
                scope.recover("header")
                continue
            header = parse_header(raw, 5.2e-8)
            log(f"CH{channel}: N={header['N']} -> {header['N']*4/1e6:.2f} MB")
            for chunk in (1024 * 1024, 8 * 1024 * 1024):
                session.write(f"CHANnel{channel}:DATA:VALues?")
                try:
                    received, elapsed = read_block(
                        session, chunk, config["instrument"]["binary_expect_termination"], 180.0)
                    log(f"CH{channel} chunk={chunk//1024//1024} MiB: "
                        f"{received/elapsed/1e6:.3f} MB/s")
                except Exception as exc:
                    log(f"CH{channel} chunk={chunk//1024//1024} MiB FAILED: "
                        f"{type(exc).__name__}: {str(exc)[:80]}")
                    try:
                        scope.recover("transfer")
                        log("  link recovered")
                    except Exception as exc2:
                        log(f"  recovery failed: {str(exc2)[:90]}")
                        return
    finally:
        try:
            scope.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
