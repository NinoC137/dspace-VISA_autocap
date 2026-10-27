#!/usr/bin/env python3
"""Pull MSO58B C:/zys/*.tss through the Pi and move each completed file locally."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from uuid import uuid4

from common import load_config

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STAGE_NAME = "_tek_tss_transfer"
REMOTE_SCRIPT = "/root/tek_pull_tss.py"
SOURCE = "C:/zys"


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value):
    if (not isinstance(value, str) or not value or ":" in value or
            "\\" in value or value.startswith("/") or
            any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("清单路径不安全：%r" % value)
    return Path(*value.split("/"))


def build_reuse_index(archive_root):
    """Index existing copies, preferring files backed by a completed manifest."""
    candidates = sorted(archive_root.glob("run_*/transfer_manifest.json"), reverse=True)
    candidates.append(archive_root / "transfer_manifest.json")
    reusable = []
    seen = set()
    known_paths = set()
    for manifest_path in candidates:
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("source") != SOURCE or
                    manifest.get("count") != len(manifest["files"])):
                raise ValueError("来源或数量不符")
        except (OSError, ValueError, KeyError, TypeError) as error:
            print("忽略无效的历史清单 %s：%s" % (manifest_path, error), flush=True)
            continue
        for item in manifest["files"]:
            try:
                relative = safe_relative(item["relative"])
                local = (archive_root / safe_relative(item["reused_from"])
                         if item.get("reused_from") else manifest_path.parent / relative)
                # A file named by a manifest must never fall back to the
                # weaker unmanifested-file rule if its recorded hash fails.
                known_paths.add(local)
                expected_size = item["bytes"]
                expected_hash = item["sha256"]
                if (not isinstance(expected_size, int) or expected_size < 0 or
                        not isinstance(expected_hash, str) or
                        not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or
                        not local.is_file() or local.stat().st_size != expected_size or
                        sha256_file(local) != expected_hash):
                    continue
                key = (relative.as_posix(), expected_size,
                       item.get("modified_date"), item.get("modified_time"))
                if key in seen:
                    continue
                seen.add(key)
                reusable.append({"relative": relative.as_posix(),
                                 "local_relative": local.relative_to(archive_root).as_posix(),
                                 "bytes": expected_size, "sha256": expected_hash,
                                 "modified_date": item.get("modified_date"),
                                 "modified_time": item.get("modified_time"),
                                 "basis": "manifest"})
            except (OSError, ValueError, KeyError, TypeError):
                continue
    # A failed batch can leave complete, atomically moved .tss files without a
    # final manifest. Include those too; the Pi still checks their path/size.
    for local in sorted(archive_root.rglob("*.tss"), reverse=True):
        if local in known_paths or not local.is_file() or local.is_symlink():
            continue
        parts = local.relative_to(archive_root).parts
        if re.fullmatch(r"run_\d{8}_\d{6}_[0-9a-f]{6}", parts[0]):
            parts = parts[1:]
        if not parts:
            continue
        relative = Path(*parts).as_posix()
        try:
            size = local.stat().st_size
            key = (relative, size, None, None)
            if key in seen:
                continue
            digest = sha256_file(local)
        except OSError:
            continue
        seen.add(key)
        reusable.append({"relative": relative,
                         "local_relative": local.relative_to(archive_root).as_posix(),
                         "bytes": size, "sha256": digest,
                         "modified_date": None, "modified_time": None,
                         "basis": "local_file"})
    return {"source": SOURCE, "files": reusable}


def move_ready(stage, target):
    moved = 0
    for source in sorted(stage.rglob("*.tss")):
        if not source.is_file():
            continue
        relative = source.relative_to(stage)
        destination = target / relative
        if destination.exists():
            raise FileExistsError("目标已存在，未覆盖：%s" % destination)
        size = source.stat().st_size
        before = sha256_file(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)  # Same F: volume; never replace a target.
        after = sha256_file(destination)
        if after != before or destination.stat().st_size != size:
            raise IOError("搬运后文件校验失败：%s" % destination)
        moved += 1
        print("已搬运 %s（%d 字节，SHA-256 %s）" % (relative, size, after), flush=True)
    return moved


def validate_complete(stage, target):
    manifest_path = stage / "transfer_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("树莓派未生成完成清单：%s" % manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest["files"]
    if manifest["source"] != SOURCE or manifest["count"] != len(files):
        raise ValueError("传输清单来源或数量不符")
    total = 0
    copied_count = 0
    reused_count = 0
    for item in files:
        relative = safe_relative(item["relative"])
        if relative.suffix.lower() != ".tss":
            raise ValueError("清单路径不安全：%s" % relative)
        if item.get("status") == "reused":
            local = ROOT / "Tek-scope" / safe_relative(item["reused_from"])
            reused_count += 1
        else:
            if item.get("status") not in (None, "copied"):
                raise ValueError("未知的传输状态：%s" % item.get("status"))
            local = target / relative
            copied_count += 1
        if not local.is_file():
            raise FileNotFoundError("缺少目标文件：%s" % local)
        size = local.stat().st_size
        digest = sha256_file(local)
        if size != item["bytes"] or digest != item["sha256"]:
            raise IOError("目标文件与示波器下载清单不符：%s" % local)
        total += size
    if total != manifest["total_bytes"]:
        raise IOError("总字节数与下载清单不符")
    if (manifest.get("copied_count", copied_count) != copied_count or
            manifest.get("reused_count", reused_count) != reused_count):
        raise ValueError("传输清单的新增/复用数量不符")
    (target / "transfer_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(files), copied_count, reused_count, total


def main():
    config = load_config(HERE / "bench_config.json")
    instrument = config["instrument"]
    if (instrument.get("transport") != "tek_socket" or
            instrument.get("expected_model") != "MSO58B" or
            instrument.get("expected_serial") != "C070122"):
        raise RuntimeError("请先在台架中选择并切换至 MSO58B C070122")
    remote = instrument["remote"]
    ssh = remote["ssh_executable"]
    if not Path(ssh).is_file():
        raise FileNotFoundError("找不到 SSH 程序：%s" % ssh)

    run_id = "run_%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), uuid4().hex[:6])
    share_root = Path(remote["local_share_root"])
    stage = share_root / STAGE_NAME / run_id
    target = ROOT / "Tek-scope" / run_id
    stage.mkdir(parents=True)
    target.mkdir(parents=True)
    reuse_index = build_reuse_index(ROOT / "Tek-scope")
    (stage / "reuse_index.json").write_text(
        json.dumps(reuse_index, ensure_ascii=False, indent=2), encoding="utf-8")
    remote_stage = remote["share_root"].rstrip("/") + "/" + STAGE_NAME + "/" + run_id
    command = "%s %s copy --stage %s" % (remote["python"], REMOTE_SCRIPT, remote_stage)
    print("示波器目录：C:/zys（递归选取 .tss）", flush=True)
    print("本批目标：%s" % target, flush=True)
    print("本地已核验 %d 个可复用文件；示波器目录核对后只下载新增文件。" %
          len(reuse_index["files"]), flush=True)
    print("连接树莓派并等待示波器独占锁；读取期间请勿并行采集或截图。", flush=True)

    process = subprocess.Popen(
        [ssh, "-o", "BatchMode=yes", remote["host"], command],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    reader_done = threading.Event()

    def relay():
        try:
            for line in process.stdout:
                print(line, end="", flush=True)
        finally:
            reader_done.set()

    threading.Thread(target=relay, daemon=True).start()
    move_error = None
    while process.poll() is None:
        if move_error is None:
            try:
                move_ready(stage, target)
            except Exception as error:
                # Do not interrupt FILESystem:READFile mid-response: that can
                # wedge the instrument's VXI-11 service. Let the Pi drain it.
                move_error = error
                print("本地搬运等待示波器读取结束后报错：%s" % error, flush=True)
        time.sleep(1)
    reader_done.wait(timeout=5)
    if move_error is not None:
        raise move_error
    move_ready(stage, target)
    if process.returncode != 0:
        raise RuntimeError("树莓派下载中止（退出码 %s）；已搬运的完整文件保留在 %s" %
                           (process.returncode, target))
    count, copied, reused, total = validate_complete(stage, target)
    print("完成：目录共 %d 个 .tss，新增 %d、复用 %d，%d 字节；清单和 SHA-256 已核对。" %
          (count, copied, reused, total), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Tek 文件拉取失败：%s" % error, file=sys.stderr, flush=True)
        raise SystemExit(1)
