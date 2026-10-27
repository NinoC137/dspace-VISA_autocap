"""Native MXO PNGs of one frozen record at several zoom spans.

Only temporary display layout commands are sent. No acquisition/timebase/channel
writes are allowed here. The original layout is restored even after a failed image.
"""
from datetime import datetime, timezone
import html
import io
import json
import math
from pathlib import Path
import time

import h5py
from PIL import Image, ImageDraw, ImageFont

from common import atomic_json, selected_channels, sha256, time_label


def checked_png(data):
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 32*1024**2:
        raise ValueError("Invalid PNG screenshot")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or not (64 <= image.width <= 8192 and 64 <= image.height <= 8192):
            raise ValueError("Invalid screenshot dimensions")
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.convert("RGB")


def annotate(image, destination, lines):
    # Append a footer; never draw over instrument pixels or waveform details.
    font = ImageFont.load_default()
    for candidate in ("C:/Windows/Fonts/msyh.ttc", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(candidate, 22)
            break
        except OSError:
            pass
    wrapped = []
    draw = ImageDraw.Draw(image)
    for line in lines:
        current = ""
        for char in line:
            if draw.textlength(current+char, font=font) > image.width-40:
                wrapped.append(current)
                current = ""
            current += char
        wrapped.append(current)
    output = Image.new("RGB", (image.width, image.height+24+34*len(wrapped)), "#142432")
    output.paste(image, (0, 0))
    draw = ImageDraw.Draw(output)
    for n, line in enumerate(wrapped):
        draw.text((20, image.height+12+34*n), line, font=font, fill="white")
    partial = destination.with_suffix(".png.partial")
    output.save(partial, format="PNG")
    partial.replace(destination)


def capture_screenshots(scope, directory, case, axis):
    """Return an explicit complete/failed report; keep already verified files."""
    cfg = scope.config["screenshots"]
    if not cfg["enabled"]:
        return dict(status="disabled", images=[])
    directory = Path(directory)
    folder = directory/"screenshots"
    folder.mkdir(exist_ok=True)
    report = dict(status="pending", kind="native_scope_zoom", images=[], context=case,
                  channels=selected_channels(scope.config), requested_spans_s=cfg["spans_s"],
                  horizontal_divisions=10, axis=axis, restore_errors=[])
    saved, layout, touched = {}, None, False
    timeout = scope.session.timeout
    headers = {}
    try:
        # Keep each metadata operation bounded rather than inheriting the waveform timeout.
        scope.session.timeout = scope.config["instrument"]["query_timeout_ms"]
        if int(scope.query("STATus:OPERation:CONDition?")) & 16:
            raise RuntimeError("Screenshots require the completed frozen acquisition")
        stamp = scope.query("ACQuire:HISTory:ISODate?")
        if axis.get("acquisition_stamp", stamp) != stamp:
            raise RuntimeError("Screenshot acquisition differs from exported data")
        report["acquisition_stamp"] = stamp
        for name in report["channels"]:
            headers[name] = scope.query(f"CHANnel{name[2:]}:DATA:HEADer?")
        for cmd in ("LAYout:ACTive", "SYSTem:DISPlay:UPDate", "DISPlay:DIAGram:GRID",
                    "DISPlay:DIAGram:LABels", "DISPlay:DIAGram:XFIXed"):
            saved[cmd] = scope.query(cmd+"?")
        for i in range(1, 5):
            if scope.query(f"LAYout{i}:ENABle?").strip().upper() in ("0", "OFF"):
                layout = i
                break
        if layout is None:
            raise RuntimeError("All four scope layouts are occupied; free one tab for screenshots")
        prefix = f"LAYout{layout}"
        zoom = prefix+":ZOOM1"
        touched = True
        scope.session.write(prefix+":ENABle ON")
        scope.session.write(prefix+':LABel "Bench screenshots"')
        # Firmware 2.8 returns some numeric keys quoted but rejects quoted inputs.
        scope.session.write(prefix+':DIAGram1:SOURce '+','.join('C'+n[2:] for n in report['channels']))
        scope.session.write(zoom+":ENABle ON")
        scope.session.write(zoom+":SOURce 1")
        scope.session.write(zoom+":HORizontal:MODE ABS")
        scope.session.write(zoom+":VERTical:MODE REL")
        scope.session.write(zoom+":VERTical:RELative:SPAN 100")
        scope.session.write(zoom+":VERTical:RELative:POSition 50")
        scope.query("*OPC?")
        zoom_diagram = int(scope.query(zoom+":DIAG?").strip('"'))
        for cmd in ("SYSTem:DISPlay:UPDate ON", "DISPlay:DIAGram:GRID ON",
                    "DISPlay:DIAGram:LABels ON", "DISPlay:DIAGram:XFIXed ON",
                    prefix+":NODE1:CHILdren2:CONTent:TYPE NONE",
                    prefix+":NODE1:CHILdren1:CONTent:TYPE DIAG",
                    prefix+f":NODE1:CHILdren1:CONTent:ID {zoom_diagram}"):
            scope.session.write(cmd)
        scope.query("*OPC?")
        scope.errors()
        center = axis["t0_s"] + (axis["N"]-1)*axis["dt_s"]*cfg["center_fraction"]
        case_values = case.get("Case", case)
        case_text = ", ".join(f"{k}={case_values[k]}" for k in ("Id", "VL", "Xi", "Ud6", "Ud7", "Phi") if k in case_values)
        for index, requested in enumerate(cfg["spans_s"], 1):
            scope.session.write(zoom+f":HORizontal:ABSolute:SPAN {requested:.15g}")
            scope.session.write(zoom+f":HORizontal:ABSolute:POSition {center:.15g}")
            if scope.query("*OPC?") != "1":
                raise RuntimeError("Zoom did not complete")
            actual = float(scope.query(zoom+":HORizontal:ABSolute:SPAN?"))
            actual_center = float(scope.query(zoom+":HORizontal:ABSolute:POSition?"))
            tolerance = max(2*axis["dt_s"], requested*1e-6)
            if (not math.isfinite(actual) or not math.isfinite(actual_center) or
                    abs(actual-requested) > tolerance or abs(actual_center-center) > tolerance):
                raise ValueError(f"Zoom readback mismatch: requested {requested}, got {actual}, center {actual_center}")
            scope.errors()
            time.sleep(cfg["settle_s"])
            if int(scope.query("STATus:OPERation:CONDition?")) & 16:
                raise RuntimeError("Acquisition restarted during screenshots")
            scope.session.timeout = round(cfg["timeout_s"]*1000)
            if hasattr(scope.session, "screenshot_bytes"):
                data = scope.recoverable("screenshot", scope.session.screenshot_bytes)
            else:
                data = bytes(scope.recoverable("screenshot", lambda: scope.session.query_binary_values(
                    "HCOPy:DATA?", datatype="B", container=bytes, header_fmt="ieee", expect_termination=True)))
            scope.session.timeout = scope.config["instrument"]["query_timeout_ms"]
            image = checked_png(data)
            for name, header in headers.items():
                if scope.query(f"CHANnel{name[2:]}:DATA:HEADer?") != header:
                    raise RuntimeError("Waveform header changed during screenshots")
            if scope.query("ACQuire:HISTory:ISODate?") != stamp:
                raise RuntimeError("Acquisition changed during screenshots")
            scope.errors()
            stem = f"{index:02d}_span_{time_label(requested)}_"+'-'.join(report['channels'])
            raw, marked = folder/(stem+"_raw.png"), folder/(stem+"_marked.png")
            partial = raw.with_suffix(".png.partial")
            partial.write_bytes(data)
            partial.replace(raw)
            utc = datetime.now(timezone.utc).isoformat()
            entry = dict(requested_span_s=requested, actual_span_s=actual,
                         actual_s_per_div=actual/10, center_s=actual_center,
                         start_s=actual_center-actual/2, stop_s=actual_center+actual/2,
                         screenshot_utc=utc, raw=raw.name, marked=marked.name,
                         raw_sha256=sha256(raw), width=image.width, height=image.height)
            annotate(image, marked, [
                f"ZOOM VIEW: {time_label(requested)} total / 10 div = {time_label(requested/10)}/div | "+', '.join(report['channels']),
                f"Actual span={actual:.9g} s | center={actual_center:.9g} s relative to trigger | native scope pixels",
                (case_text or "Capture")+" | "+directory.name,
                "Screenshot UTC: "+utc+" | Top toolbar timebase belongs to the full acquisition."])
            entry["marked_sha256"] = sha256(marked)
            report["images"].append(entry)
            atomic_json(folder/"index.json", report)
        report["status"] = "complete"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    finally:
        if touched:
            # Continue restoration if one command fails; report every failure.
            commands = [("LAYout:ACTive", saved["LAYout:ACTive"]), (f"LAYout{layout}:ENABle", "OFF")]
            commands += [(k, v) for k, v in saved.items() if k != "LAYout:ACTive"]
            for cmd, value in commands:
                try:
                    scope.session.write(cmd+" "+value.strip('"'))
                except Exception as exc:
                    report["restore_errors"].append(f"{cmd}: {exc}")
            try:
                scope.query("*OPC?")
                scope.errors()
                for cmd, value in saved.items():
                    if scope.query(cmd+"?") != value:
                        raise RuntimeError("Display restore mismatch: "+cmd)
            except Exception as exc:
                report["restore_errors"].append(str(exc))
            if report["restore_errors"]:
                report["status"] = "failed"
        scope.session.timeout = timeout
    atomic_json(folder/"index.json", report)
    cards = ''.join('<section><h2>'+html.escape(time_label(e['requested_span_s']))+
                   ' total</h2><a href="'+e['raw']+'">Original PNG</a><br><img src="'+
                   e['marked']+'"></section>' for e in report['images'])
    (folder/"index.html").write_text('<!doctype html><meta charset="utf-8"><title>MXO38 screenshots</title>'
        '<style>body{background:#15212d;color:#fff;font:16px sans-serif;margin:24px}img{max-width:100%}'
        'a{color:#8edaff}section{margin:32px 0}</style><h1>MXO38 · '+html.escape(directory.name)+
        '</h1><p>Status: '+html.escape(report['status'])+'</p><p>'+html.escape(report.get('error',''))+
        '</p>'+cards, encoding="utf-8")
    return report


def _tek_screenshot(scope, directory, case, raw):
    """Save native Tek PNGs of the same frozen record, including event-centered zooms.

    PC socket SCPI changes display zoom only. Pi VXI-11 `scope-shot` reads the
    actual screen pixels, after each zoom has completed. No new acquisition is
    started and no waveform samples are rendered into a synthetic image.
    """
    import glob
    import os
    import subprocess
    import time as _time

    cfg = scope.config["screenshots"]
    if not cfg["enabled"]:
        return {"status": "disabled", "backend": "tek", "images": []}
    directory = Path(directory)
    (directory/"screenshots").mkdir(exist_ok=True)
    remote = (scope.config.get("instrument") or {}).get("remote") or {}
    ssh = remote.get("ssh_executable", "ssh")
    host = remote.get("host")
    share = remote.get("share_root", "/mnt/scope_results")
    local_root = remote.get("local_share_root")
    report = {"status": "pending", "backend": "tek", "kind": "native_frozen_record_zoom",
              "images": [], "restore_errors": [], "requested_spans_s": cfg["spans_s"]}
    if not host or not local_root:
        return dict(report, status="failed", error="instrument.remote.host / local_share_root missing")
    case_values = case.get("Case", case)
    policy = case_values.get("Note")
    dynamic = policy in ("fixed90", "fixed180", "adaptive")
    # Fixed-angle grid cases use the same frozen-record views as steady xi cases.
    steady = policy in ("steady_best", "fixed_phase_5deg_grid")
    if not dynamic and not steady:
        return dict(report, status="failed", error="Unexpected xi screenshot policy: " + str(policy))

    with h5py.File(raw, "r") as h:
        ch1 = h["channels/CH1"]
        t0 = float(ch1.attrs["t0_s"])
        span = (int(ch1.attrs["N"])-1)*float(ch1.attrs["dt_s"])
    if span <= 0:
        return dict(report, status="failed", error="Invalid saved waveform span")
    events = case.get("Events") or []
    switches = [e for e in events if e.get("action") == "xi_switch"]
    if len(switches) != (2 if dynamic else 0):
        return dict(report, status="failed", error="Missing/extra xi switch events in capture context")
    acquisition = [json.loads(line) for line in (directory/"events.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    acquisition = [e for e in acquisition if e.get("state") == "acquiring"]
    if len(acquisition) != 1:
        return dict(report, status="failed", error="Cannot identify acquisition start")
    acquire_utc = datetime.fromisoformat(acquisition[0]["utc"].replace("Z", "+00:00"))
    views = []
    if dynamic:
        requested = float(cfg["spans_s"][0])
        if len(cfg["spans_s"]) != 1 or not .15 <= requested <= .30:
            return dict(report, status="failed", error="Dynamic native zoom requires one 150-300 ms span")
        previous_xi = float(case_values["Xi"])
        for number, event in enumerate(switches, 1):
            event_utc = datetime.fromisoformat(event["utc"].replace("Z", "+00:00"))
            event_offset = (event_utc-acquire_utc).total_seconds()
            if not requested/2 <= event_offset <= span-requested/2:
                return dict(report, status="failed", error="Xi switch lies outside the saved zoomable record")
            views.append(dict(name=f"xi_switch_{number:02d}_200ms.png", span_s=requested,
                              position_percent=100*event_offset/span, event_offset_s=event_offset,
                              xi_before=previous_xi, xi_after=float(event["xi"])))
            previous_xi = float(event["xi"])
    else:
        for requested in cfg["spans_s"]:
            requested = float(requested)
            if not 0 < requested < span:
                return dict(report, status="failed", error="Steady zoom span exceeds saved record")
            views.append(dict(name=f"steady_{round(requested*1000):d}ms.png", span_s=requested,
                              position_percent=100*float(cfg.get("center_fraction", .5))))

    zoom = "DISplay:WAVEView1:ZOOM:ZOOM1"
    keys = {"state": zoom+":STATe", "position": zoom+":HORizontal:POSition",
            "winscale": zoom+":HORizontal:WINSCALe"}
    saved = {}
    touched = False
    capture_stamp = scope.query("ACQuire:NUMACq?")
    def assert_frozen(stage="native_screenshot"):
        state = scope.query("ACQuire:STATE?").strip()
        count = scope.query("ACQuire:NUMACq?").strip()
        report.setdefault("freeze_checks", []).append(dict(
            stage=stage, state=state, acquisitions=count, expected_acquisitions=capture_stamp))
        if state != "0" or count != capture_stamp:
            raise RuntimeError("Frozen acquisition changed during native screenshots: "
                               f"stage={stage}, state={state}, acquisitions={count}, expected={capture_stamp}")

    def save_screen(target, label):
        prefix = ("bench_"+directory.name[-20:]+"_"+label).replace(":", "_")
        started = _time.time()
        command = "scope-shot --scope mso58b -o %s -f %s" % (share, prefix)
        done = subprocess.run([ssh, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                               host, command], capture_output=True, text=True, timeout=180)
        if done.returncode:
            raise RuntimeError((done.stdout+done.stderr).strip()[:300] or f"scope-shot rc={done.returncode}")
        produced = sorted(glob.glob(os.path.join(local_root, prefix+"_*.png")), key=os.path.getmtime)
        produced = [p for p in produced if os.path.getmtime(p) >= started-5]
        if not produced:
            raise RuntimeError("scope-shot returned no current PNG")
        data = Path(produced[-1]).read_bytes()
        image = checked_png(data)
        target.parent.mkdir(exist_ok=True)
        partial = target.with_suffix(".png.partial")
        partial.write_bytes(data)
        partial.replace(target)
        return {"file": str(target.relative_to(directory)), "sha256": sha256(target),
                "bytes": len(data), "width": image.width, "height": image.height}

    # This unit requests only the native screen as it currently appears.
    # Do not change zoom state, zoom scales, display position, or acquisition.
    if cfg.get("view_mode") == "current":
        report.update(kind="native_current_screen", requested_spans_s=[])
        try:
            assert_frozen("before_current_screen")
            report["images"].append(dict(kind="current_screen", **save_screen(directory/"screen.png", "current")))
            assert_frozen("after_current_screen")
            report["status"] = "complete"
        except Exception as exc:
            report.update(status="failed", error=str(exc))
        finally:
            atomic_json(directory/"screenshots"/"index.json", report)
        return report

    try:
        assert_frozen()
        horizontal_position = float(scope.query("HORizontal:POSition?"))
        if abs(horizontal_position) > 1.5:
            raise RuntimeError("Tek trigger position changed from the 0% acquisition-start setting")
        report["horizontal_trigger_position_percent"] = horizontal_position
        for name, key in keys.items():
            saved[name] = scope.query(key+"?").strip()
        # Force an unzoomed first image even when the operator left zoom enabled.
        touched = True
        scope.write(keys["state"]+" OFF")
        scope.query("*OPC?")
        assert_frozen()
        report["images"].append(dict(kind="overview", **save_screen(directory/"screen.png", "overview")))
        scope.write(keys["state"]+" ON")
        for number, view in enumerate(views, 1):
            scope.write(keys["winscale"]+" %.12g" % (view["span_s"]/10))
            scope.write(keys["position"]+" %.9g" % view["position_percent"])
            if scope.query("*OPC?").strip() != "1":
                raise RuntimeError("Tek display zoom did not complete")
            actual_scale = float(scope.query(keys["winscale"]+"?"))
            actual_position = float(scope.query(keys["position"]+"?"))
            if not .75*view["span_s"] <= 10*actual_scale <= 1.3*view["span_s"] or \
                    abs(actual_position-view["position_percent"]) > 1.5:
                raise RuntimeError("Tek zoom readback does not cover requested event window")
            scope.errors()
            assert_frozen()
            _time.sleep(float(cfg.get("settle_s", .5)))
            entry = dict(kind="event_zoom" if dynamic else "steady_zoom", **view)
            entry.update(save_screen(directory/"screenshots"/view["name"], f"zoom{number}"))
            entry.update(actual_span_s=10*actual_scale, actual_position_percent=actual_position,
                         acquisition_stamp=capture_stamp)
            assert_frozen()
            report["images"].append(entry)
        report["status"] = "complete"
    except Exception as exc:
        report.update(status="failed", error=str(exc))
    finally:
        if touched:
            for name in ("winscale", "position", "state"):
                try:
                    scope.write(keys[name]+" "+saved[name])
                except Exception as exc:
                    report["restore_errors"].append(f"{name}: {exc}")
            try:
                scope.query("*OPC?")
                assert_frozen()
                if int(float(scope.query(keys["state"]+"?"))) != int(float(saved["state"])) or \
                        abs(float(scope.query(keys["position"]+"?"))-float(saved["position"])) > .01 or \
                        abs(float(scope.query(keys["winscale"]+"?"))-float(saved["winscale"])) > 1e-9:
                    raise RuntimeError("Tek display zoom restore readback mismatch")
            except Exception as exc:
                report["restore_errors"].append(str(exc))
            if report["restore_errors"]:
                report["status"] = "failed"
        atomic_json(directory/"screenshots"/"index.json", report)
    return report


def export_screenshots(scope, directory, case, raw):
    if getattr(scope, "backend", "") == "tek":
        return _tek_screenshot(scope, directory, case, raw)
    with h5py.File(raw, "r") as h:
        first = h["channels/"+selected_channels(scope.config)[0]]
        axis = {key: first.attrs[key].item() for key in ("t0_s", "dt_s", "N")}
        if "acquisition_stamp" in h.attrs:
            axis["acquisition_stamp"] = h.attrs["acquisition_stamp"]
    return capture_screenshots(scope, directory, case, axis)
