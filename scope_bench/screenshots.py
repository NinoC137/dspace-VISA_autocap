"""Native MXO PNGs of one frozen record at several zoom spans.

Only temporary display layout commands are sent. No acquisition/timebase/channel
writes are allowed here. The original layout is restored even after a failed image.
"""
from datetime import datetime, timezone
import html
import io
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


def export_screenshots(scope, directory, case, raw):
    with h5py.File(raw, "r") as h:
        first = h["channels/"+selected_channels(scope.config)[0]]
        axis = {key: first.attrs[key].item() for key in ("t0_s", "dt_s", "N")}
        if "acquisition_stamp" in h.attrs:
            axis["acquisition_stamp"] = h.attrs["acquisition_stamp"]
    return capture_screenshots(scope, directory, case, axis)
