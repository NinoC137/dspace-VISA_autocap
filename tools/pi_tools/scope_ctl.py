#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scope_ctl.py —— MXO38 参数控制命令行（树莓派侧，经 R&S VISA / USB）。

一次调用 = 打开 VISA 会话 → 执行 → 关闭，不常驻、不占仪器。
每个"写"操作都做：读旧值 → 写 → 读新值 → 查 SYST:ERR:ALL?，把 before/after/errors 一起报告。

用法示例：
  scope-ctl state                              # 全量只读快照
  scope-ctl chan 2 --scale 0.2 --offset 0.7    # CH2 0.2/div，偏置 0.7
  scope-ctl chan 3 on                          # 打开 CH3
  scope-ctl tb --scale 1m                      # 1 ms/div（支持 m/u/n/k/M 后缀）
  scope-ctl trig --mode NORM --source C2 --level 0.1 --slope POS
  scope-ctl acq --type AVER --count 16
  scope-ctl display --grid off --persist off
  scope-ctl view clean                         # 纯波形界面预设
  scope-ctl meas 1 --type RMS --source C2      # 配置测量槽
  scope-ctl meas read                          # 读所有已启用测量值
  scope-ctl run | stop | single
  scope-ctl screenshot -f before_tuning
  scope-ctl settings save my_setup             # 保存到仪器 /home/storage/userData/settings/
  scope-ctl settings snapshot /mnt/scope_results/snap.json   # 保存可查询的全部参数到 JSON
  scope-ctl scpi "CHAN2:SCAL?" "ACQ:SRAT?"    # 只读查询
  scope-ctl scpi --write "CHAN2:SCAL 0.5"     # 任意写命令（需 --write）
  scope-ctl --json ...                         # 机器可读输出（MCP 用）
"""
import argparse
import ctypes
import json
import os
import re
import struct
import sys
import time
import urllib.request

sys.path.insert(0, "/root")
import rsvisa                                   # noqa: E402
from rsvisa import Instrument, VisaError, _lib  # noqa: E402

VI_ATTR_TMO_VALUE = 0x3FFF001A
_lib.viSetAttribute.argtypes = [rsvisa.ViSession, ctypes.c_uint32, ctypes.c_size_t]
_lib.viSetAttribute.restype = ctypes.c_int32

LIVE = "http://127.0.0.1:8080"
DEFAULT_OUT = "/mnt/scope_results"
SETTINGS_DIR = "/home/storage/userData/settings"

# 危险命令：会复位/关机/删文件/升级固件，必须显式 --i-know 才放行
DANGEROUS = re.compile(r"^\s*(\*RST|SYST(em)?:(PRES(et)?|SHUT(down)?|EXIT|FW)|MMEM(ory)?:DEL(ete)?|CAL(ibration)?(:ALL)?\s)", re.I)


# ----------------------------------------------------------------- 工具函数
def si(value):
    """'500m' -> 0.5, '2u' -> 2e-6, '10k' -> 1e4；非数字原样返回（如 AUTO/ON）。"""
    if value is None:
        return None
    s = str(value).strip()
    m = re.fullmatch(r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([fpnumkMG]?)", s)
    if not m:
        return s
    num = float(m.group(1))
    scale = dict(f=1e-15, p=1e-12, n=1e-9, u=1e-6, m=1e-3, k=1e3, M=1e6, G=1e9).get(m.group(2), 1.0)
    v = num * scale
    return repr(v) if v != int(v) or abs(v) >= 1e15 else str(int(v))


def onoff(value):
    v = str(value).strip().lower()
    if v in ("1", "on", "true", "yes"):
        return "ON"
    if v in ("0", "off", "false", "no"):
        return "OFF"
    raise SystemExit(f"需要 on/off，收到 {value!r}")


def chan_index(text):
    m = re.fullmatch(r"(?:C|CH|CHAN)?([1-8])", str(text).strip(), re.I)
    if not m:
        raise SystemExit(f"通道应为 1-8 / C1-C8，收到 {text!r}")
    return int(m.group(1))


def source_name(text):
    """C2 / CH2 / 2 -> 'C2'；其余（EXT、LINE、M1、SPEC…）原样大写。"""
    t = str(text).strip()
    m = re.fullmatch(r"(?:C|CH|CHAN)?([1-8])", t, re.I)
    return f"C{m.group(1)}" if m else t.upper()


def live_pause(seconds):
    try:
        urllib.request.urlopen(f"{LIVE}/api/status", timeout=1.5).read()
    except Exception:
        return False
    try:
        urllib.request.urlopen(f"{LIVE}/api/pause?seconds={seconds}", timeout=2).read()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def live_resume():
    try:
        urllib.request.urlopen(f"{LIVE}/api/pause?seconds=0", timeout=2).read()
    except Exception:
        pass


# ----------------------------------------------------------------- 仪器封装
class Scope:
    def __init__(self, timeout_s):
        self.ins = Instrument(timeout_ms=int(timeout_s * 1000))
        self.set_timeout(timeout_s)
        self.log = []            # 每一步 {cmd, before, after, ok}
        self.writes = 0
        self.clears = 0
        self.timeouts = 0

    def set_timeout(self, seconds):
        _lib.viSetAttribute(self.ins.vi, VI_ATTR_TMO_VALUE, int(seconds * 1000))

    def clear(self):
        """VISA device clear：中止挂起的传输。一笔未读完的应答会让仪器对后续所有命令都超时，
        所以任何一次超时之后都必须 clear，否则整条链路就"哑"了。"""
        _lib.viClear(self.ins.vi)
        self.clears += 1
        time.sleep(0.3)

    def q(self, cmd, default=None, max_bytes=65536):
        try:
            return self.ins.query(cmd, max_bytes=max_bytes)
        except VisaError as exc:
            # 实测：仪器"不支持/不可用"的���询只会超时并在错误队列留下 -397，链路本身仍然健康；
            # 只有一笔大传输中途中断才会把链路打哑。所以先用错误队列探一下，探不通再 clear。
            self.timeouts += 1
            try:
                self.ins.query("SYST:ERR:ALL?")
            except VisaError:
                self.clear()
            if default is not None:
                return default
            raise

    def errors(self):
        text = self.q("SYST:ERR:ALL?", default='?,"error queue unreadable"')
        return [] if text.startswith("0,") else [text]

    def w(self, cmd):
        try:
            self.ins.write(cmd)
        except VisaError:
            self.clear()
            raise
        self.writes += 1
        errs = self.errors()
        entry = dict(cmd=cmd, errors=errs)
        self.log.append(entry)
        return errs

    def wait_opc(self, deadline_s, poll_s=0.25):
        """发 *OPC 后轮询 *ESR? 的 bit0，直到完成或超时。永远不用阻塞式 *OPC?：
        单次采集在 NORM 触发下可能永远等不到触发，阻塞查询超时就会把链路打哑。"""
        self.ins.write("*OPC")
        t_end = time.time() + deadline_s
        self.set_timeout(2.0)
        while time.time() < t_end:
            esr = self.q("*ESR?", default="")
            if esr.isdigit() and int(esr) & 1:
                return True
            time.sleep(poll_s)
        return False

    def set(self, header, value, query=None):
        """header 形如 'CHAN2:SCAL'；读旧值→写→读新值。"""
        query = query or header + "?"
        before = self.q(query, default="(n/a)")
        errs = self.w(f"{header} {value}")
        after = self.q(query, default="(n/a)")
        entry = self.log[-1]
        entry.update(before=before, after=after, ok=not errs)
        return entry

    def block(self, cmd):
        try:
            return self.ins.query_block(cmd)
        except VisaError:
            self.clear()
            raise

    def close(self):
        self.ins.close()


# ----------------------------------------------------------------- 只读快照
def snapshot(s, all_channels=False):
    s.set_timeout(2.0)
    d = {}
    d["idn"] = s.q("*IDN?")
    d["options"] = s.q("*OPT?", default="?")
    d["channels"] = {}
    for n in range(1, 9):
        on = s.q(f"CHAN{n}:STAT?", default="?") == "1"
        c = dict(on=on)
        if on or all_channels:
            c.update(
                scale_per_div=s.q(f"CHAN{n}:SCAL?", default="?"),
                range_full=s.q(f"CHAN{n}:RANG?", default="?"),
                offset=s.q(f"CHAN{n}:OFFS?", default="?"),
                position_div=s.q(f"CHAN{n}:POS?", default="?"),
                coupling=s.q(f"CHAN{n}:COUP?", default="?"),
                bandwidth_hz=s.q(f"CHAN{n}:BAND?", default="?"),
                impedance=s.q(f"CHAN{n}:IMP?", default="?"),
                invert=s.q(f"CHAN{n}:INV?", default="?"),
                skew_s=s.q(f"CHAN{n}:SKEW:TIME?", default="?"),
                probe=probe_info(s, n),
            )
        d["channels"][f"CH{n}"] = c
    d["timebase"] = dict(
        scale_s_per_div=s.q("TIM:SCAL?"), range_s=s.q("TIM:RANG?"), position_s=s.q("TIM:HOR:POS?"),
        reference_pct=s.q("TIM:REF?"), divisions=s.q("TIM:DIV?"), roll=s.q("TIM:ROLL:ENAB?"),
    )
    d["acquisition"] = dict(
        type=s.q("ACQ:TYPE?"), average_count=s.q("ACQ:COUN?"), sample_rate_hz=s.q("ACQ:SRAT?"),
        sample_rate_mode=s.q("ACQ:SRAT:MODE?"), resolution_s=s.q("ACQ:RES?"),
        record_length=s.q("ACQ:POIN?"), record_length_mode=s.q("ACQ:POIN:MODE?"),
        record_length_max=s.q("ACQ:POIN:MAX?"), interpolation=s.q("ACQ:INT?"),
        segmented=s.q("ACQ:SEGM:STAT?"), acquisitions_done=s.q("ACQ:CURR?"),
    )
    d["trigger"] = dict(
        mode=s.q("TRIG:MODE?"), source=s.q("TRIG:EVEN1:SOUR?"), type=s.q("TRIG:EVEN1:TYPE?"),
        edge_slope=s.q("TRIG:EVEN1:EDGE:SLOP?"), holdoff_mode=s.q("TRIG:HOLD:MODE?"),
        coupling=s.q("TRIG:ANED:COUP?", default="?"), levels={},
    )
    for n in range(1, 9):
        if d["channels"][f"CH{n}"]["on"] or d["trigger"]["source"] == f"C{n}":
            d["trigger"]["levels"][f"C{n}"] = s.q(f"TRIG:EVEN1:LEV{n}?", default="?")
    d["display"] = dict(
        grid=s.q("DISP:DIAG:GRID?"), fine_grid=s.q("DISP:DIAG:FIN?"), labels=s.q("DISP:DIAG:LAB?"),
        crosshair=s.q("DISP:DIAG:CROS?"), style=s.q("DISP:DIAG:STYL?"), y_fixed=s.q("DISP:DIAG:YFIX?"),
        x_fixed=s.q("DISP:DIAG:XFIX?"), persistence=s.q("DISP:PERS:STAT?"), persistence_time_s=s.q("DISP:PERS:TIME?"),
        persistence_infinite=s.q("DISP:PERS:INF?"), intensity_pct=s.q("DISP:INT?"),
        backlight_pct=s.q("DISP:BACK?"), remote_display_update=s.q("SYST:DISP:UPD?"),
        dialog_font=s.q("DISP:DIAL:FONT?"), result_font=s.q("DISP:RES:FONT?"),
        keyboard_locked=s.q("SYST:KLOC?"), message_shown=s.q("SYST:DISP:MESS:STAT?"),
    )
    d["layout"] = dict(
        tabs=s.q("LAY1:COUN?", default="?"), active=s.q("LAY1:ACT?", default="?"),
        diagram1_source=s.q("LAY1:DIAG1:SOUR?", default="?"), result_position=s.q("LAY1:RPOS?", default="?"),
        result_h_ratio=s.q("LAY1:RES1:HOR:RAT?", default="?"), result_v_ratio=s.q("LAY1:RES1:VERT:RAT?", default="?"),
    )
    d["measurements"] = {}
    for n in range(1, 9):
        en = s.q(f"MEAS{n}:ENAB?", default="?")
        m = dict(enabled=en == "1")
        if en == "1":
            m.update(type=s.q(f"MEAS{n}:MAIN?", default="?"), source=s.q(f"MEAS{n}:SOUR?", default="?"),
                     statistics=s.q(f"MEAS{n}:STAT?", default="?"))
            src = re.match(r"C([1-8])", m["source"] or "")
            if src and d["channels"][f"CH{src.group(1)}"]["on"]:
                m["value"] = s.q(f"MEAS{n}:RES:ACT?", default="?")
            else:
                m["value"] = "(source off)"
        d["measurements"][f"MEAS{n}"] = m
    d["cursor1"] = dict(on=s.q("CURS1:STAT?", default="?"), function=s.q("CURS1:FUNC?", default="?"),
                        source=s.q("CURS1:SOUR?", default="?"))
    d["error_queue"] = s.errors()
    return d


def probe_info(s, n):
    """PROBe:SETup:ATTenuation:MANual? 在 AUTO 模式下返回 -397 Not available（超时），只在 MAN 模式查。"""
    info = dict(name=s.q(f"PROB{n}:SET:NAME?", default="?"), detected=s.q(f"PROB{n}:SET:STAT?", default="?"),
                unit=s.q(f"PROB{n}:SET:ATT:UNIT?", default="?"), atten_mode=s.q(f"PROB{n}:SET:ATT:MODE?", default="?"))
    info["atten_manual"] = s.q(f"PROB{n}:SET:ATT:MAN?", default="?") if info["atten_mode"] == "MAN" else "(auto)"
    return info


def flat(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from flat(v, key + ".")
        else:
            yield key, v


# ----------------------------------------------------------------- USB 复位
def usb_device_dir(vendor="0aad"):
    base = "/sys/bus/usb/devices"
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name, "idVendor")
        try:
            if open(path).read().strip() == vendor:
                return os.path.join(base, name)
        except OSError:
            continue
    return None


def usb_reauthorize(wait_s=5.0):
    """端口级复位：sysfs authorized 0→1 让内核对示波器重新枚举。
    实测（2026-09-20）可救回 viClear 都救不回的"哑"链路，不用给示波器断电。"""
    dev = usb_device_dir()
    if not dev:
        return dict(ok=False, error="lsusb 里没有 0aad（R&S）设备：检查 USB 线 / 示波器电源")
    with open(os.path.join(dev, "authorized"), "w") as f:
        f.write("0")
    time.sleep(2.0)
    with open(os.path.join(dev, "authorized"), "w") as f:
        f.write("1")
    time.sleep(wait_s)
    return dict(ok=True, sysfs=dev, note="已重新枚举 USB 设备")


def cmd_recover(a):
    """先试 viClear，不行再 USB 重枚举，最后报告 *IDN? 是否恢复。"""
    steps = []
    def try_idn(label):
        try:
            s = Scope(3.0)
        except VisaError as exc:
            steps.append(dict(step=label, open_error=str(exc)[:120]))
            return None
        try:
            for _ in range(2):
                try:
                    idn = s.ins.query("*IDN?")
                    steps.append(dict(step=label, idn=idn, ok=True))
                    return s
                except VisaError:
                    s.clear()
            steps.append(dict(step=label, ok=False))
            s.close()
            return None
        except Exception:
            s.close()
            raise
    s = try_idn("viClear")
    if s is None:
        steps.append(dict(step="usb_reauthorize", **usb_reauthorize()))
        s = try_idn("after_usb_reauthorize")
    if s is None:
        return dict(ok=False, steps=steps, next="软件手段用尽：请给示波器断电重启，再 scope-ctl info")
    errs = s.errors()
    s.close()
    return dict(ok=True, steps=steps, error_queue_drained=errs)


# ----------------------------------------------------------------- 各子命令
def cmd_info(s, a):
    return dict(idn=s.q("*IDN?"), options=s.q("*OPT?", default="?"), errors=s.errors(),
                acq_done=s.q("ACQ:CURR?", default="?"))


def cmd_state(s, a):
    return snapshot(s, all_channels=a.all)


def cmd_chan(s, a):
    results = []
    chans = range(1, 9) if a.channel.lower() == "all" else [chan_index(a.channel)]
    for n in chans:
        h = f"CHAN{n}"
        if a.switch:
            results.append(s.set(f"{h}:STAT", onoff(a.switch)))
        if a.scale is not None:
            results.append(s.set(f"{h}:SCAL", si(a.scale)))
        if a.range is not None:
            results.append(s.set(f"{h}:RANG", si(a.range)))
        if a.offset is not None:
            results.append(s.set(f"{h}:OFFS", si(a.offset)))
        if a.position is not None:
            results.append(s.set(f"{h}:POS", si(a.position)))
        if a.coupling:
            results.append(s.set(f"{h}:COUP", a.coupling.upper()))
        if a.bandwidth is not None:
            results.append(s.set(f"{h}:BAND", si(a.bandwidth)))
        if a.invert:
            results.append(s.set(f"{h}:INV", onoff(a.invert)))
        if a.skew is not None:
            results.append(s.set(f"{h}:SKEW:TIME", si(a.skew)))
        if a.unit:
            results.append(s.set(f"PROB{n}:SET:ATT:UNIT", a.unit.upper()))
        if a.atten is not None:
            results.append(s.set(f"PROB{n}:SET:ATT:MODE", "MAN"))
            results.append(s.set(f"PROB{n}:SET:ATT:MAN", si(a.atten)))
    if not results:
        return {f"CH{n}": snapshot_channel(s, n) for n in chans}
    return dict(changes=results)


def snapshot_channel(s, n):
    s.set_timeout(2.0)
    return dict(on=s.q(f"CHAN{n}:STAT?") == "1", scale=s.q(f"CHAN{n}:SCAL?"), offset=s.q(f"CHAN{n}:OFFS?"),
                position=s.q(f"CHAN{n}:POS?"), coupling=s.q(f"CHAN{n}:COUP?"), bandwidth=s.q(f"CHAN{n}:BAND?"),
                probe=probe_info(s, n))


def cmd_tb(s, a):
    r = []
    if a.scale is not None:
        r.append(s.set("TIM:SCAL", si(a.scale)))
    if a.range is not None:
        r.append(s.set("TIM:RANG", si(a.range)))
    if a.position is not None:
        r.append(s.set("TIM:HOR:POS", si(a.position)))
    if a.reference is not None:
        r.append(s.set("TIM:REF", si(a.reference)))
    if a.roll:
        r.append(s.set("TIM:ROLL:ENAB", a.roll.upper()))
    return dict(changes=r) if r else snapshot(s)["timebase"]


def cmd_acq(s, a):
    r = []
    if a.type:
        r.append(s.set("ACQ:TYPE", a.type.upper()))
    if a.count is not None:
        r.append(s.set("ACQ:COUN", int(a.count)))
    if a.srate_mode:
        r.append(s.set("ACQ:SRAT:MODE", a.srate_mode.upper()))
    if a.srate is not None:
        r.append(s.set("ACQ:SRAT", si(a.srate)))
    if a.points_mode:
        r.append(s.set("ACQ:POIN:MODE", a.points_mode.upper()))
    if a.points is not None:
        mode = s.q("ACQ:POIN:MODE?")
        if mode != "MAN":
            r.append(s.set("ACQ:POIN:MODE", "MAN"))     # AUTO 模式下 ACQ:POIN 只读
        r.append(s.set("ACQ:POIN", int(float(si(a.points)))))
    if a.interp:
        r.append(s.set("ACQ:INT", a.interp.upper()))
    return dict(changes=r) if r else snapshot(s)["acquisition"]


def cmd_runctl(s, a):
    cmd = dict(run="RUN", stop="STOP", single="SINGle")[a.action]
    errs = s.w(cmd)
    out = dict(sent=cmd, errors=errs)
    if a.action == "single":
        done = s.wait_opc(a.wait)
        out["completed"] = done
        if not done:
            trig = s.q("TRIG:MODE?", default="?")
            out["note"] = (f"{a.wait:.0f} s 内没有完成：触发模式 {trig}，可能一直等不到触发。"
                           "仪器仍在等待；可用 `scope-ctl stop`、`scope-ctl trig --force` 或改回 --mode AUTO。")
    return out


def cmd_autoscale(s, a):
    before = snapshot(s)
    errs = s.w("AUToscale")
    time.sleep(3)
    return dict(sent="AUToscale", errors=errs, before=dict(channels=before["channels"], timebase=before["timebase"]),
                after=snapshot(s))


def cmd_trig(s, a):
    r = []
    if a.mode:
        r.append(s.set("TRIG:MODE", a.mode.upper()))
    if a.type:
        r.append(s.set("TRIG:EVEN1:TYPE", a.type.upper()))
    if a.source:
        r.append(s.set("TRIG:EVEN1:SOUR", source_name(a.source)))
    if a.slope:
        r.append(s.set("TRIG:EVEN1:EDGE:SLOP", a.slope.upper()))
    if a.level is not None:
        src = source_name(a.source) if a.source else s.q("TRIG:EVEN1:SOUR?")
        m = re.fullmatch(r"C([1-8])", src)
        if not m:
            raise SystemExit(f"当前触发源 {src} 不是模拟通道，请用 --source 指定 C1-C8 后再设电平")
        r.append(s.set(f"TRIG:EVEN1:LEV{m.group(1)}", si(a.level)))
    if a.holdoff_mode:
        r.append(s.set("TRIG:HOLD:MODE", a.holdoff_mode.upper()))
    if a.find_level:
        r.append(dict(cmd="TRIG:FINDlevel", errors=s.w("TRIG:FINDlevel")))
    if a.force:
        r.append(dict(cmd="TRIG:FORCe", errors=s.w("TRIG:FORCe")))
    return dict(changes=r) if r else snapshot(s)["trigger"]


def cmd_display(s, a):
    r = []
    for flag, header in (("grid", "DISP:DIAG:GRID"), ("fine_grid", "DISP:DIAG:FIN"), ("labels", "DISP:DIAG:LAB"),
                         ("crosshair", "DISP:DIAG:CROS"), ("y_fixed", "DISP:DIAG:YFIX"), ("x_fixed", "DISP:DIAG:XFIX"),
                         ("persist", "DISP:PERS:STAT"), ("infinite", "DISP:PERS:INF"),
                         ("remote_update", "SYST:DISP:UPD"), ("lock_keys", "SYST:KLOC")):
        v = getattr(a, flag)
        if v:
            r.append(s.set(header, onoff(v)))
    if a.style:
        r.append(s.set("DISP:DIAG:STYL", a.style.upper()))
    if a.persist_time is not None:
        r.append(s.set("DISP:PERS:TIME", si(a.persist_time)))
    if a.intensity is not None:
        r.append(s.set("DISP:INT", si(a.intensity)))
    if a.backlight is not None:
        r.append(s.set("DISP:BACK", si(a.backlight)))
    if a.dialog_font is not None:
        r.append(s.set("DISP:DIAL:FONT", int(a.dialog_font)))
    if a.result_font is not None:
        r.append(s.set("DISP:RES:FONT", int(a.result_font)))
    if a.result_position:
        r.append(s.set("LAY1:RPOS", a.result_position.upper()))
    if a.message is not None:
        if a.message == "":
            r.append(s.set("SYST:DISP:MESS:STAT", "OFF"))
        else:
            r.append(dict(cmd="SYST:DISP:MESS", errors=s.w(f'SYST:DISP:MESS "{a.message}"')))
            r.append(s.set("SYST:DISP:MESS:STAT", "ON"))
    if a.clear:
        r.append(dict(cmd="DISP:CLR", errors=s.w("DISP:CLR")))
    if a.persist_reset:
        r.append(dict(cmd="DISP:PERS:RES", errors=s.w("DISP:PERS:RES")))
    if a.toolbar_deselect:
        r.append(dict(cmd="DISP:TOOL:DES", errors=s.w("DISP:TOOL:DES")))
    return dict(changes=r) if r else snapshot(s)["display"]


VIEW_PRESETS = {
    # 纯波形：网格+标签保留，去掉十字光标与余辉，矢量线，关掉屏幕提示
    "clean": [("DISP:DIAG:GRID", "ON"), ("DISP:DIAG:FIN", "OFF"), ("DISP:DIAG:LAB", "ON"), ("DISP:DIAG:CROS", "OFF"),
              ("DISP:PERS:STAT", "OFF"), ("DISP:DIAG:STYL", "VECT"), ("SYST:DISP:MESS:STAT", "OFF"), ("DISP:INT", "60")],
    # 完全裸屏：连网格标签都去掉，适合截图贴报告
    "bare": [("DISP:DIAG:GRID", "OFF"), ("DISP:DIAG:FIN", "OFF"), ("DISP:DIAG:LAB", "OFF"), ("DISP:DIAG:CROS", "OFF"),
             ("DISP:PERS:STAT", "OFF"), ("DISP:DIAG:STYL", "VECT"), ("SYST:DISP:MESS:STAT", "OFF")],
    # 分析：细网格、十字光标、点显示，短余辉便于看抖动
    "analysis": [("DISP:DIAG:GRID", "ON"), ("DISP:DIAG:FIN", "ON"), ("DISP:DIAG:LAB", "ON"), ("DISP:DIAG:CROS", "ON"),
                 ("DISP:PERS:STAT", "ON"), ("DISP:PERS:TIME", "0.05"), ("DISP:DIAG:STYL", "VECT")],
    # 恢复到 2026-09-20 交付时观察到的出厂样式
    "default": [("DISP:DIAG:GRID", "ON"), ("DISP:DIAG:FIN", "ON"), ("DISP:DIAG:LAB", "ON"), ("DISP:DIAG:CROS", "ON"),
                ("DISP:PERS:STAT", "ON"), ("DISP:PERS:TIME", "0.05"), ("DISP:PERS:INF", "OFF"), ("DISP:DIAG:STYL", "VECT"),
                ("DISP:INT", "50"), ("DISP:DIAG:YFIX", "ON"), ("DISP:DIAG:XFIX", "OFF")],
}


def cmd_view(s, a):
    if a.preset == "list":
        return {k: [f"{h} {v}" for h, v in items] for k, items in VIEW_PRESETS.items()}
    r = [s.set(h, v) for h, v in VIEW_PRESETS[a.preset]]
    if a.result_position:
        r.append(s.set("LAY1:RPOS", a.result_position.upper()))
    return dict(preset=a.preset, changes=r)


def cmd_meas(s, a):
    if a.slot == "read":
        s.set_timeout(2.0)
        out = {}
        for n in range(1, 9):
            if s.q(f"MEAS{n}:ENAB?", default="0") != "1":
                continue
            src = s.q(f"MEAS{n}:SOUR?", default="?")
            m = re.match(r"C([1-8])", src)
            if m and s.q(f"CHAN{m.group(1)}:STAT?", default="0") != "1":
                out[f"MEAS{n}"] = dict(type=s.q(f"MEAS{n}:MAIN?"), source=src, value="(source off)")
                continue
            out[f"MEAS{n}"] = dict(type=s.q(f"MEAS{n}:MAIN?"), source=src, value=s.q(f"MEAS{n}:RES:ACT?", default="(no result)"))
        return out
    n = int(a.slot)
    if not 1 <= n <= 8:
        raise SystemExit("测量槽 1-8")
    r = []
    if a.switch:
        r.append(s.set(f"MEAS{n}:ENAB", onoff(a.switch)))
    if a.source:
        r.append(s.set(f"MEAS{n}:SOUR", source_name(a.source)))
    if a.type:
        r.append(s.set(f"MEAS{n}:MAIN", a.type.upper()))
    if a.stats:
        r.append(s.set(f"MEAS{n}:STAT", onoff(a.stats)))
    if a.reset_stats:
        r.append(dict(cmd=f"MEAS{n}:STAT:RES", errors=s.w(f"MEAS{n}:STAT:RES")))
    if a.clear:
        r.append(dict(cmd=f"MEAS{n}:CLE", errors=s.w(f"MEAS{n}:CLE")))
    if not r:
        return snapshot(s)["measurements"][f"MEAS{n}"]
    return dict(changes=r)


def cmd_screenshot(s, a):
    os.makedirs(a.outdir, exist_ok=True)
    s.set_timeout(30)
    t0 = time.time()
    data = s.block(":HCOPy:DATA?")
    name = f"{a.prefix}_{time.strftime('%Y%m%d-%H%M%S')}.png"
    path = os.path.join(a.outdir, name)
    with open(path, "wb") as f:
        f.write(data)
    w = h = 0
    try:
        w, h = struct.unpack(">II", data[16:24])
    except Exception:
        pass
    return dict(path=path, windows_path="F:\\ZYS_HYBASE\\automation\\scope_results\\" + name if a.outdir == DEFAULT_OUT else None,
                bytes=len(data), size=f"{w}x{h}", seconds=round(time.time() - t0, 2))


def cmd_settings(s, a):
    if a.action == "list":
        s.set_timeout(5)
        raw = s.q(f"MMEM:CAT? '{SETTINGS_DIR}'", default="?")
        files = re.findall(r'"([^"]+)"', raw)
        return dict(dir=SETTINGS_DIR, entries=[f for f in files if not f.startswith((".,", "..,"))])
    if a.action in ("save", "recall"):
        if not a.name:
            raise SystemExit("需要名字：settings save <name>")
        path = a.name if "/" in a.name else f"{SETTINGS_DIR}/{a.name}.dfl"
        cmd = "MMEM:SAV" if a.action == "save" else "MMEM:RCL"
        s.set_timeout(30)
        errs = s.w(f"{cmd} '{path}'")
        out = dict(sent=f"{cmd} '{path}'", errors=errs)
        if a.action == "recall":
            out["completed"] = s.wait_opc(30)
        return out
    if a.action == "snapshot":
        if not a.name:
            raise SystemExit("需要输出文件：settings snapshot /mnt/scope_results/xxx.json")
        snap = snapshot(s, all_channels=True)
        snap["_meta"] = dict(saved_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"), tool="scope_ctl.py")
        with open(a.name, "w") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        return dict(path=a.name, keys=len(list(flat(snap))))
    if a.action == "restore":
        if not a.name or not os.path.exists(a.name):
            raise SystemExit("需要 snapshot 生成的 JSON 文件路径")
        snap = json.load(open(a.name))
        r = []
        for ch, c in snap["channels"].items():
            n = ch[2:]
            if "scale_per_div" in c:
                for key, header in (("scale_per_div", "SCAL"), ("offset", "OFFS"), ("position_div", "POS"),
                                    ("coupling", "COUP"), ("bandwidth_hz", "BAND"), ("invert", "INV")):
                    if c.get(key) not in (None, "?"):
                        r.append(s.set(f"CHAN{n}:{header}", c[key]))
            r.append(s.set(f"CHAN{n}:STAT", "ON" if c["on"] else "OFF"))
        tb = snap["timebase"]
        r.append(s.set("TIM:SCAL", tb["scale_s_per_div"]))
        r.append(s.set("TIM:HOR:POS", tb["position_s"]))
        r.append(s.set("TIM:REF", tb["reference_pct"]))
        acq = snap["acquisition"]
        r.append(s.set("ACQ:TYPE", acq["type"]))
        r.append(s.set("ACQ:COUN", acq["average_count"]))
        r.append(s.set("ACQ:SRAT:MODE", acq["sample_rate_mode"]))
        if acq["sample_rate_mode"] == "MAN":
            r.append(s.set("ACQ:SRAT", acq["sample_rate_hz"]))
        r.append(s.set("ACQ:POIN:MODE", acq["record_length_mode"]))
        if acq["record_length_mode"] == "MAN":
            r.append(s.set("ACQ:POIN", acq["record_length"]))
        r.append(s.set("ACQ:INT", acq["interpolation"]))
        tr = snap["trigger"]
        r.append(s.set("TRIG:MODE", tr["mode"]))
        r.append(s.set("TRIG:EVEN1:TYPE", tr["type"]))
        r.append(s.set("TRIG:EVEN1:SOUR", tr["source"]))
        r.append(s.set("TRIG:EVEN1:EDGE:SLOP", tr["edge_slope"]))
        for src, lev in tr.get("levels", {}).items():
            if lev != "?":
                r.append(s.set(f"TRIG:EVEN1:LEV{src[1]}", lev))
        dp = snap["display"]
        for key, header in (("grid", "DISP:DIAG:GRID"), ("fine_grid", "DISP:DIAG:FIN"), ("labels", "DISP:DIAG:LAB"),
                            ("crosshair", "DISP:DIAG:CROS"), ("style", "DISP:DIAG:STYL"), ("persistence", "DISP:PERS:STAT"),
                            ("persistence_time_s", "DISP:PERS:TIME"), ("persistence_infinite", "DISP:PERS:INF"),
                            ("intensity_pct", "DISP:INT"), ("y_fixed", "DISP:DIAG:YFIX"), ("x_fixed", "DISP:DIAG:XFIX")):
            r.append(s.set(header, dp[key]))
        for slot, m in snap["measurements"].items():
            n = slot[4:]
            if m["enabled"]:
                r.append(s.set(f"MEAS{n}:SOUR", m["source"]))
                r.append(s.set(f"MEAS{n}:MAIN", m["type"]))
            r.append(s.set(f"MEAS{n}:ENAB", "ON" if m["enabled"] else "OFF"))
        bad = [x for x in r if x.get("errors")]
        return dict(applied=len(r), failed=bad)
    raise SystemExit("settings 子命令：list | save <name> | recall <name> | snapshot <file.json> | restore <file.json>")


def cmd_scpi(s, a):
    out = []
    s.set_timeout(a.timeout)
    for cmd in a.commands:
        is_query = cmd.strip().endswith("?") or "? " in cmd
        if is_query:
            t = time.time()
            try:
                val = s.q(cmd, max_bytes=1 << 20)
                out.append(dict(cmd=cmd, value=val, seconds=round(time.time() - t, 3)))
            except VisaError as exc:
                out.append(dict(cmd=cmd, error=str(exc), errors=s.errors()))
        else:
            if not a.write:
                out.append(dict(cmd=cmd, skipped="写命令需要 --write"))
                continue
            if DANGEROUS.match(cmd) and not a.i_know:
                out.append(dict(cmd=cmd, skipped="危险命令（复位/关机/删除/校准），需要 --i-know"))
                continue
            out.append(dict(cmd=cmd, errors=s.w(cmd)))
    return dict(results=out)


def cmd_errors(s, a):
    return dict(errors=s.errors(), count=s.q("SYST:ERR:COUN?", default="?"))


# ----------------------------------------------------------------- 参数解析
def build_parser():
    p = argparse.ArgumentParser(prog="scope-ctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="JSON 输出（供 MCP/脚本用）")
    p.add_argument("--timeout", type=float, default=5.0, help="VISA 单次 I/O 超时（秒），默认 5")
    p.add_argument("--no-handoff", action="store_true", help="不向 scope-live 网页服务请求让出设备")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="IDN / 选件 / 错误队列")
    x = sub.add_parser("state", help="全量只读快照")
    x.add_argument("--all", action="store_true", help="关闭的通道也读全部参数")

    x = sub.add_parser("chan", help="通道：开关 / 量程 / 偏置 / 耦合 / 带宽 / 探头单位")
    x.add_argument("channel", help="1-8 / C2 / all")
    x.add_argument("switch", nargs="?", choices=["on", "off", "ON", "OFF"], help="打开或关闭")
    x.add_argument("--scale", help="每格值（V/div 或 A/div，支持 500m）")
    x.add_argument("--range", help="满屏值 = scale×10")
    x.add_argument("--offset", help="偏置（通道单位）")
    x.add_argument("--position", help="垂直位置（格）")
    x.add_argument("--coupling", help="DC | DCLimit | AC（DC=50Ω, DCLimit=1MΩ）")
    x.add_argument("--bandwidth", help="带宽限制，Hz（如 20M / 200M / FULL）")
    x.add_argument("--invert", choices=["on", "off"])
    x.add_argument("--skew", help="通道时延（秒）")
    x.add_argument("--unit", help="探头单位 V | A（写 PROBe:SETup:ATTenuation:UNIT）")
    x.add_argument("--atten", help="探头手动衰减/换算系数（如电流探头 0.2 = 0.2 V/A 的倒数按仪器定义）")

    x = sub.add_parser("tb", help="时基：s/div、总时长、水平位置、参考点")
    x.add_argument("--scale", help="s/div（支持 1m / 20u）")
    x.add_argument("--range", help="总时长 = scale×10")
    x.add_argument("--position", help="水平位置（秒）")
    x.add_argument("--reference", help="参考点百分比 0-100")
    x.add_argument("--roll", help="AUTO | OFF")

    x = sub.add_parser("acq", help="采集：类型 / 平均次数 / 采样率 / 记录长度 / 插值")
    x.set_defaults(wait=0)
    x.add_argument("--type", help="SAMPle | AVERage | ENVelope | PDETect")
    x.add_argument("--count", help="平均次数")
    x.add_argument("--srate-mode", dest="srate_mode", help="AUTO | MANual")
    x.add_argument("--srate", help="采样率 Hz（MAN 模式下有效，支持 20M）")
    x.add_argument("--points-mode", dest="points_mode", help="AUTO | MANual")
    x.add_argument("--points", help="记录长度（会自动切 MAN）")
    x.add_argument("--interp", help="LINear | SINX | SMHD")

    for name in ("run", "stop", "single"):
        x = sub.add_parser(name, help={"run": "连续采集", "stop": "停止", "single": "单次采集并等待完成（轮询，不阻塞链路）"}[name])
        x.set_defaults(action=name)
        if name == "single":
            x.add_argument("--wait", type=float, default=20.0, help="最多等待秒数（默认 20）")
    sub.add_parser("autoscale", help="仪器自动设置（会大改通道/时基/触发！）")

    x = sub.add_parser("trig", help="触发：模式 / 源 / 类型 / 斜率 / 电平 / 释抑")
    x.add_argument("--mode", help="AUTO | NORMal | FREerun")
    x.add_argument("--source", help="C1-C8 / EXT / LINE")
    x.add_argument("--type", help="EDGE | WIDTh | RUNT | WINDow | TIMeout | INTerval | SLEWrate | SETHold | STATe | PATTern")
    x.add_argument("--slope", help="POSitive | NEGative | EITHer")
    x.add_argument("--level", help="触发电平（触发源通道单位）")
    x.add_argument("--holdoff-mode", dest="holdoff_mode", help="OFF | TIME | EVENts | RANDom | AUTO")
    x.add_argument("--find-level", dest="find_level", action="store_true", help="让仪器自动找电平")
    x.add_argument("--force", action="store_true", help="强制触发一次")

    x = sub.add_parser("display", help="显示：网格 / 标签 / 十字线 / 余辉 / 亮度 / 消息 / 远控刷屏")
    for f in ("grid", "fine-grid", "labels", "crosshair", "y-fixed", "x-fixed", "persist", "infinite", "remote-update", "lock-keys"):
        x.add_argument(f"--{f}", dest=f.replace("-", "_"), choices=["on", "off"])
    x.add_argument("--style", help="VECTors | DOTS")
    x.add_argument("--persist-time", dest="persist_time", help="余辉时间（秒）")
    x.add_argument("--intensity", help="波形亮度 %%")
    x.add_argument("--backlight", help="背光 %%")
    x.add_argument("--dialog-font", dest="dialog_font", help="对话框字号")
    x.add_argument("--result-font", dest="result_font", help="结果表字号")
    x.add_argument("--result-position", dest="result_position", help="结果表位置 LEFT | RIGHt | TOP | BOTTom")
    x.add_argument("--message", help="在屏幕上显示一条文字；空字符串 '' 清除")
    x.add_argument("--clear", action="store_true", help="清除波形显示（DISP:CLR）")
    x.add_argument("--persist-reset", dest="persist_reset", action="store_true")
    x.add_argument("--toolbar-deselect", dest="toolbar_deselect", action="store_true")

    x = sub.add_parser("view", help="界面预设：clean / bare / analysis / default / list")
    x.add_argument("preset", choices=list(VIEW_PRESETS) + ["list"])
    x.add_argument("--result-position", dest="result_position")

    x = sub.add_parser("meas", help="测量槽 1-8 配置，或 meas read 读取全部")
    x.add_argument("slot", help="1-8 或 read")
    x.add_argument("switch", nargs="?", choices=["on", "off", "ON", "OFF"])
    x.add_argument("--type", help="PDELta(峰峰) MEAN RMS AMPLitude FREQuency PERiod MAXimum MINimum RTIMe FTIMe PDCYcle NDCYcle HIGH LOW ...")
    x.add_argument("--source", help="C1-C8")
    x.add_argument("--stats", choices=["on", "off"])
    x.add_argument("--reset-stats", dest="reset_stats", action="store_true")
    x.add_argument("--clear", action="store_true", help="清空该槽")

    x = sub.add_parser("screenshot", help="整屏 PNG 存到 PC 共享盘")
    x.add_argument("-o", "--outdir", default=DEFAULT_OUT)
    x.add_argument("-f", "--prefix", default="screen")

    x = sub.add_parser("settings", help="仪器设置文件 save/recall/list；参数 JSON snapshot/restore")
    x.add_argument("action", choices=["list", "save", "recall", "snapshot", "restore"])
    x.add_argument("name", nargs="?")

    x = sub.add_parser("scpi", help="原始 SCPI：以 ? 结尾为查询；写命令需 --write")
    x.add_argument("commands", nargs="+")
    x.add_argument("--write", action="store_true")
    x.add_argument("--i-know", dest="i_know", action="store_true", help="放行复位/关机/删除类危险命令")

    sub.add_parser("errors", help="读取并清空错误队列")
    sub.add_parser("recover", help="链路救援：viClear → USB 端口重枚举 → 复查 *IDN?")
    return p


HANDLERS = dict(info=cmd_info, state=cmd_state, chan=cmd_chan, tb=cmd_tb, acq=cmd_acq, run=cmd_runctl, stop=cmd_runctl,
                single=cmd_runctl, autoscale=cmd_autoscale, trig=cmd_trig, display=cmd_display, view=cmd_view,
                meas=cmd_meas, screenshot=cmd_screenshot, settings=cmd_settings, scpi=cmd_scpi, errors=cmd_errors)


def render(result, indent=0):
    pad = "  " * indent
    if isinstance(result, dict) and "changes" in result:
        for c in result["changes"]:
            mark = "OK " if not c.get("errors") else "ERR"
            if "before" in c:
                print(f"{pad}{mark} {c['cmd']:<36} {c['before']} -> {c['after']}")
            else:
                print(f"{pad}{mark} {c['cmd']}")
            for e in c.get("errors", []):
                print(f"{pad}      ! {e}")
        rest = {k: v for k, v in result.items() if k != "changes"}
        if rest:
            render(rest, indent)
        return
    if isinstance(result, dict):
        for k, v in result.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                render(v, indent + 1)
            else:
                print(f"{pad}{k:<24} {v}")
    elif isinstance(result, list):
        for item in result:
            if isinstance(item, (dict, list)):
                render(item, indent + 1)
                print()
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{result}")


def main():
    args = build_parser().parse_args()
    paused = False if args.no_handoff else live_pause(600)
    t0 = time.time()
    if args.cmd == "recover":
        try:
            out = cmd_recover(args)
        finally:
            if paused:
                live_resume()
        out["seconds"] = round(time.time() - t0, 2)
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["ok"] else 2
    try:
        s = Scope(args.timeout)
    except VisaError as exc:
        msg = str(exc)
        hint = None
        if "BFFF0011" in msg:
            hint = "VISA 资源被占用或不存在：先 `scope-web status`、`ps aux | grep -E 'scope_live|pi_agent|scope_capture'`，再 `lsusb | grep 0aad`"
        out = dict(ok=False, error=msg, hint=hint)
        print(json.dumps(out, ensure_ascii=False) if args.json else f"打开仪器失败: {msg}\n{hint or ''}")
        return 2
    try:
        result = HANDLERS[args.cmd](s, args)
        pending = s.errors() if args.cmd not in ("errors",) else []
        out = dict(ok=True, cmd=args.cmd, result=result, writes=s.writes, query_timeouts=s.timeouts,
                   link_clears=s.clears, seconds=round(time.time() - t0, 2), live_paused=paused, pending_errors=pending)
        failed = [c for c in s.log if c.get("errors")]
        if failed:
            out["ok"] = False
            out["failed"] = failed
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        else:
            render(result)
            if failed:
                print(f"\n有 {len(failed)} 条命令报错，见上方 ERR 行")
            if pending:
                print("残留错误队列:", pending)
            print(f"[{args.cmd}: {s.writes} 次写入, {out['seconds']} s"
                  f"{', 链路 clear ' + str(s.clears) + ' 次' if s.clears else ''}"
                  f"{', 网页服务已临时让出' if paused else ''}]")
        return 0 if out["ok"] else 1
    except VisaError as exc:
        s.clear()
        out = dict(ok=False, cmd=args.cmd, error=str(exc), log=s.log, link_clears=s.clears,
                   hint="已执行 VISA device clear；若仍超时请 `scope-ctl info` 复查，必要时给示波器断电重启")
        print(json.dumps(out, ensure_ascii=False, indent=1) if args.json else f"VISA 错误: {exc}\n{out['hint']}")
        return 2
    finally:
        try:
            s.close()
        finally:
            if paused:
                live_resume()


if __name__ == "__main__":
    sys.exit(main())
