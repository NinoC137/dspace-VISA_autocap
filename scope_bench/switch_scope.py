#!/usr/bin/env python3
"""switch_scope.py —— 一键切换示波器（PC 侧 + 树莓派侧一起切）。

它做三件事：
  1. 把所选档案里的 instrument 段补丁写进 scope_bench/bench_config.json
     （其余 timing / acquisition / channels / analysis / screenshots 一个字都不动，
      所以切换不会丢实验设置）；
  2. ssh 到树莓派执行 `scope-use <pi_name>`，让树莓派侧的工具跟着切；
  3. 打印切换结果和下一步该敲什么。

用法:
  python switch_scope.py list
  python switch_scope.py show
  python switch_scope.py use tek                 # mso58b 的别名
  python switch_scope.py use tek --dry-run       # 只看会改什么，不落盘
  python switch_scope.py use tek --no-pi         # 不动树莓派
  python switch_scope.py use tek --json          # 机器可读输出

设计说明
--------
* instrument 段是**合并**不是替换：档案只覆盖它显式给出的键，
  这样 query_timeout_ms / export_timeout_s / min_throughput_bytes_per_s 等
  仍然沿用 bench_config.json 里已有的值。
* 写盘前先备份成 bench_config.json.bak-<时间戳>，写盘用"临时文件 + os.replace"，
  不会出现写一半的配置文件。
* 档案的 status 字段说明成熟度：
    ready       = 台架全流程可用
    config-only = 连接 / 识别 / raw 查询可用，采集导出流水线还没适配
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROFILES = os.path.join(HERE, "instrument_profiles.json")
DEFAULT_CONFIG = os.path.join(HERE, "bench_config.json")


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_json(path, value):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".bench_config.", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve(profiles, name):
    """支持别名、大小写不敏感。"""
    key = str(name or "").strip().lower()
    table = profiles["profiles"]
    if key in table:
        return key
    for k, p in table.items():
        if key in [str(a).lower() for a in p.get("aliases", [])] or key == k.lower():
            return k
    raise SystemExit("不认识的示波器 %r；可选：%s" % (
        name, ", ".join(sorted(table))))


def active_name(profiles):
    a = profiles.get("active")
    try:
        return resolve(profiles, a) if a else sorted(profiles["profiles"])[0]
    except SystemExit:
        return sorted(profiles["profiles"])[0]


def apply_profile(config, profile):
    """把档案的 instrument 补丁合并进 config（不修改入参）。返回新配置。"""
    new = json.loads(json.dumps(config))          # deep copy
    inst = new.setdefault("instrument", {})
    patch = profile.get("instrument") or {}
    for k, v in patch.items():
        if k == "remote" and isinstance(v, dict):
            remote = inst.setdefault("remote", {})
            remote.update(v)                      # remote 也逐键合并
        else:
            inst[k] = v
    return new


def diff_instrument(old, new):
    """列出 instrument 段里真正变化的键，便于回显。"""
    a = (old or {}).get("instrument", {})
    b = (new or {}).get("instrument", {})
    keys = sorted(set(a) | set(b))
    out = []
    for k in keys:
        if a.get(k) != b.get(k):
            if k == "remote" and isinstance(a.get(k), dict) and isinstance(b.get(k), dict):
                for rk in sorted(set(a[k]) | set(b[k])):
                    if a[k].get(rk) != b[k].get(rk):
                        out.append(("remote.%s" % rk, a[k].get(rk), b[k].get(rk)))
            else:
                out.append((k, a.get(k), b.get(k)))
    return out


def sync_pi(remote, pi_name, timeout_s=25):
    """ssh 到树莓派切它那边的 active 指针。

    用 `scope-use <name> --no-check`：切换和"连一下仪器"是两件事，
    加上检查的话，仪器离线时 scope-use 会返回非 0，被误判成同步失败。
    """
    exe = remote.get("ssh_executable", "ssh")
    host = remote.get("host", "")
    if not host:
        return False, "配置里没有 remote.host"
    cmd = [exe, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           host, "scope-use %s --no-check" % pi_name]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        return False, "找不到 ssh：%s" % exe
    except subprocess.TimeoutExpired:
        return False, "ssh 超时（%s）" % host
    if p.returncode != 0:
        return False, (p.stdout + p.stderr).strip()[:300]
    return True, (p.stdout or "").strip()


def cmd_list(profiles):
    act = active_name(profiles)
    print("示波器档案 : %s" % DEFAULT_PROFILES)
    for name in sorted(profiles["profiles"]):
        p = profiles["profiles"][name]
        mark = "*" if name == act else " "
        alias = ",".join(p.get("aliases", [])) or "-"
        print("  %s %-8s %-9s %s" % (mark, name, p.get("status", "?"), p.get("label", "")))
        print("     别名: %-28s 树莓派名: %s" % (alias, p.get("pi_name", "-")))
        print("     资源: %s" % p.get("instrument", {}).get("resource", "-"))
    print("* = 当前生效档（%s）" % act)
    return 0


def cmd_show(profiles, config_path):
    cfg = read_json(config_path) if os.path.exists(config_path) else {}
    act = active_name(profiles)
    inst = cfg.get("instrument", {})
    print("当前档案 : %s" % act)
    print("配置文件 : %s" % config_path)
    print("资源     : %s" % inst.get("resource", "-"))
    print("期望型号 : %s  期望序列号: %s" % (inst.get("expected_model", "-"),
                                              inst.get("expected_serial", "-")))
    print("传输     : %s" % inst.get("transport", "-"))
    remote = inst.get("remote") or {}
    if remote:
        print("树莓派   : %s  (agent %s)" % (remote.get("host", "-"), remote.get("agent", "-")))
    prof = profiles["profiles"].get(act, {})
    want = (prof.get("instrument") or {}).get("resource")
    if want and want != inst.get("resource"):
        print("注意     : 配置里的资源与当前档案不一致（档案=%s）；跑 `use %s` 同步" % (want, act))
    return 0


def cmd_use(profiles, config_path, name, dry_run=False, no_pi=False, as_json=False):
    key = resolve(profiles, name)
    profile = profiles["profiles"][key]
    old = read_json(config_path) if os.path.exists(config_path) else {"schema_version": 1}
    new = apply_profile(old, profile)
    changes = diff_instrument(old, new)
    result = {"profile": key, "label": profile.get("label"),
              "status": profile.get("status"), "config": config_path,
              "changes": [{"key": k, "from": a, "to": b} for k, a, b in changes],
              "dry_run": dry_run, "pi_synced": None}

    if not changes:
        result["note"] = "配置已经是这个档案，没有需要改的键"
    if not dry_run and changes:
        if os.path.exists(config_path):
            bak = "%s.bak-%s" % (config_path, time.strftime("%Y%m%d-%H%M%S"))
            shutil.copy2(config_path, bak)
            result["backup"] = bak
        atomic_json(config_path, new)

    if not dry_run and not no_pi:
        ok, msg = sync_pi(new.get("instrument", {}).get("remote", {}),
                          profile.get("pi_name", key))
        result["pi_synced"] = ok
        result["pi_message"] = msg

    # 把 active 指针记到档案表里，方便 list/show 显示
    if not dry_run and profiles.get("active") != key:
        profiles["active"] = key
        atomic_json(DEFAULT_PROFILES, profiles)

    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("pi_synced") is False else 0

    print("切换目标 : %s  (%s)" % (profile.get("label", key), key))
    print("成熟度   : %s" % profile.get("status", "?"))
    if changes:
        print("将修改 %d 项%s:" % (len(changes), "（--dry-run，未落盘）" if dry_run else ""))
        for k, a, b in changes:
            print("   %-28s %s  ->  %s" % (k, a, b))
    else:
        print("配置已经是这个档案，无需修改。")
    if result.get("backup"):
        print("已备份   : %s" % result["backup"])
    if result.get("pi_synced") is True:
        print("树莓派   : 已同步")
        if result.get("pi_message"):
            print("           %s" % result["pi_message"].splitlines()[0])
    elif result.get("pi_synced") is False:
        print("树莓派   : 同步失败 —— %s" % result.get("pi_message"))
    elif no_pi:
        print("树莓派   : 按要求跳过（--no-pi）")
    if profile.get("status") == "config-only":
        print("提醒     : 该档案目前只打通连接/识别/raw 查询；")
        print("           台架的采集导出流水线还没做 Tek SCPI 方言适配，先别拿它跑批测。")
    print("\n下一步   : python bench.py check --config \"%s\"" % config_path)
    return 1 if result.get("pi_synced") is False else 0


KNOWN_COMMANDS = ("list", "show", "use")


def normalize_argv(argv):
    """允许简写：`switch_scope.py tek` 等价于 `use tek`。

    文档和 switch-scope.cmd 都按简写写，所以这里必须兜住，
    否则会报 "invalid choice: 'tek'"。
    """
    argv = list(argv or [])
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--profiles", "--config"):      # 这两个全局选项带值
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if a not in KNOWN_COMMANDS:
            argv.insert(i, "use")                # 裸名字 -> use <名字>
        return argv
    return argv


def main(argv=None):
    ap = argparse.ArgumentParser(description="一键切换示波器")
    ap.add_argument("--profiles", default=DEFAULT_PROFILES)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出所有档案")
    sub.add_parser("show", help="显示当前生效的连接参数")

    p_use = sub.add_parser("use", help="切换到某个档案")
    p_use.add_argument("name")
    p_use.add_argument("--dry-run", action="store_true", help="只显示改动，不落盘")
    p_use.add_argument("--no-pi", action="store_true", help="不同步树莓派")
    p_use.add_argument("--json", action="store_true", help="以 JSON 输出")

    # argv is None when invoked from the command line: normalise sys.argv too,
    # otherwise the shorthand `switch_scope.py tek` is never expanded.
    raw = list(sys.argv[1:]) if argv is None else list(argv)
    a = ap.parse_args(normalize_argv(raw))
    if not os.path.exists(a.profiles):
        print("找不到档案表：%s" % a.profiles, file=sys.stderr)
        return 2
    profiles = read_json(a.profiles)

    if a.cmd == "use":
        return cmd_use(profiles, a.config, a.name, a.dry_run, a.no_pi, a.json)
    if a.cmd == "show":
        return cmd_show(profiles, a.config)
    return cmd_list(profiles)


if __name__ == "__main__":
    sys.exit(main())
