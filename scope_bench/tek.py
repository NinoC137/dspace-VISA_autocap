"""Tektronix MSO58B bench backend: the same surface as mxo.Mxo, different SCPI dialect.

Why this file exists
--------------------
`mxo.py` is written entirely in Rohde & Schwarz / MXO38 SCPI. The MSO58B speaks
Tektronix SCPI, which differs in almost every command family, so a per-model
adapter is unavoidable. This one implements exactly the surface `bench.py`,
`screenshots.py` and `analysis.py` consume:

    Tek(config, session=None)
    .snapshot() .errors() .link_report()
    .prepare(directory) .acquire() .export(directory, case) -> Path
    .close(stop=False)
    .session  .settings  .acquired  .acquisition_stamp

Transport
---------
Direct LAN from the PC to the instrument's Tek socket server
(`TCPIP::192.168.2.200::4000::SOCKET`), pure stdlib. The PC already has a route
to 192.168.2.0/24 via the Raspberry Pi and the Pi masquerades to eth0, so no
VISA, no SSH and no Pi process sits in the data path. Measured: connect 0.05 s,
`CURVe?` 5000 pts in 0.34 s.

Verified SCPI dialect notes (FW 2.14.3.2448) — all established on real hardware
--------------------------------------------------------------------------------
* `HORizontal:RECOrdlength` is **read-only while `HORizontal:MODe AUTO`**, and a
  write issued immediately after switching to `MANual` is silently ignored (the
  instrument jumps to its maximum record length instead). So `_set_record_length`
  waits, reads back and retries.
* `DATa:STOP` defaults to 1000000 and **caps** what `CURVe?` returns, even when
  the record is longer. The export sets STARt/STOP to the full record.
* `DATa:ENCdg RIBinary` is **big-endian signed** integers; `DATa:WIDth 2` = 16 bit.
* Calibration is `value = (raw - YOFF) * YMULT + YZERO`, from `WFMOutpre:*`.
* Unsupported headers return **nothing at all** (a full timeout) and desync the
  session, so only the headers in `VERIFIED_*` may be used. This is why the
  export parses the preamble per-field instead of splitting `WFMOutpre?`.
* Channel display is toggled with `CH<n>:DISplay {ON|OFF}`. The corresponding
  *query* is not supported on this firmware, so the backend cannot read the state
  back — it reports what it set and validates by checking that real samples came
  back for every enabled channel.
* No `ACQuire:POMemory?` / `CH<n>:OVERLoad?` equivalent, so those two MXO checks
  have no counterpart here and are simply absent.
"""
from __future__ import annotations

import json
import math
import socket
import struct
import time
from pathlib import Path

import numpy as np

from common import atomic_json, selected_channels, sha256

DEFAULT_HOST = "192.168.2.200"
DEFAULT_PORT = 4000

#: Only these may be used: anything else returns no response and desyncs the link.
VERIFIED_QUERIES = (
    "*IDN?", "*OPT?", "*ESR?", "*OPC?", "ALLEv?", "VERBose?",
    "ACQuire:STATE?", "ACQuire:MODe?", "ACQuire:NUMACq?", "ACQuire:STOPAfter?",
    "ACQuire:NUMAVg?",
    "HORizontal:MODe?", "HORizontal:RECOrdlength?", "HORizontal:SCAle?",
    "HORizontal:SAMPLERate?", "HORizontal:POSition?",
    "TRIGger:A:MODe?", "TRIGger:A:EDGE:SOUrce?", "TRIGger:STATE?",
    "DATa:SOUrce?", "DATa:ENCdg?", "DATa:WIDth?", "DATa:STARt?", "DATa:STOP?",
    "WFMOutpre?", "WFMOutpre:BYT_Nr?", "WFMOutpre:NR_Pt?", "WFMOutpre:XINCR?",
    "WFMOutpre:XZEro?", "WFMOutpre:PT_Off?", "WFMOutpre:YMUlt?", "WFMOutpre:YOFf?",
    "WFMOutpre:YZEro?", "WFMOutpre:YUNit?", "WFMOutpre:XUNit?",
    "FILESystem:CWD?", "FILESystem:DIR?", "FILESystem:LDIR?",
)
VERIFIED_WRITES = (
    "*CLS", "ACQuire:STATE", "ACQuire:MODe", "ACQuire:STOPAfter", "ACQuire:NUMAVg",
    "HORizontal:MODe", "HORizontal:RECOrdlength", "HORizontal:SCAle",
    "DATa:SOUrce", "DATa:ENCdg", "DATa:WIDth", "DATa:STARt", "DATa:STOP",
    "TRIGger:A:MODe", "CH1:DISplay", "CH2:DISplay", "CH3:DISplay", "CH4:DISplay",
    "CH5:DISplay", "CH6:DISplay", "CH7:DISplay", "CH8:DISplay",
    "CH1:COUPling", "CH2:COUPling", "CH3:COUPling", "CH4:COUPling",
    "CH5:COUPling", "CH6:COUPling", "CH7:COUPling", "CH8:COUPling",
    "SAVe:IMAGe", "SAVe:IMAGe:FILEFormat", "SAVe:IMAGe:INKSaver",
    "FILESystem:READFile", "FILESystem:DELete",
)


class TekError(RuntimeError):
    pass


def looks_like_idn(text):
    if not text or "," not in text:
        return False
    fields = text.split(",")
    return len(fields) >= 4 and all(f.isprintable() for f in fields)


def parse_socket_resource(resource):
    """`TCPIP::192.168.2.200::4000::SOCKET` -> ("192.168.2.200", 4000)"""
    parts = str(resource).split("::")
    if len(parts) < 4 or parts[0].upper() != "TCPIP" or parts[-1].upper() != "SOCKET":
        raise ValueError("tek_socket resource must look like "
                         "TCPIP::<host>::<port>::SOCKET, got %r" % resource)
    host = parts[1]
    try:
        port = int(parts[2])
    except (TypeError, ValueError):
        raise ValueError("tek_socket resource needs a numeric port, got %r" % resource)
    return host, port


class TekSocketSession:
    """Minimal SCPI session over the instrument's socket server (stdlib only)."""

    def __init__(self, resource, timeout_s=10.0):
        self.resource = str(resource)
        self.host, self.port = parse_socket_resource(self.resource)
        self.timeout = float(timeout_s)
        self.chunk_size = 65536
        self.expected_bytes = 0
        self.write_termination = "\n"
        self.read_termination = "\n"
        self.sock = None
        self._buf = bytearray()

    # ---- lifecycle ----
    def open(self, connect_timeout=6.0):
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=connect_timeout)
        self.sock.settimeout(self.timeout)
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._buf = bytearray()
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            finally:
                self.sock = None

    @property
    def connection_info(self):
        return {"transport": "tek_socket", "resource": self.resource,
                "host": self.host, "port": self.port, "protocol": "scpi-raw"}

    # ---- io ----
    def _fill(self):
        chunk = self.sock.recv(self.chunk_size)
        if not chunk:
            raise TekError("instrument closed the socket connection")
        self._buf += chunk

    def _need(self, n):
        while len(self._buf) < n:
            self._fill()

    def write(self, command):
        if self.sock is None:
            raise TekError("session is not open")
        self.sock.sendall(str(command).encode("ascii", "replace") + b"\n")

    def read_line(self):
        while True:
            i = self._buf.find(b"\n")
            if i >= 0:
                line = bytes(self._buf[:i])
                del self._buf[:i + 1]
                return line.decode("latin-1").strip()
            self._fill()

    def query(self, command, timeout_s=None):
        if self.sock is None:
            raise TekError("session is not open (call open() first)")
        old = self.sock.gettimeout()
        if timeout_s:
            self.sock.settimeout(float(timeout_s))
        try:
            self.write(command)
            return self.read_line()
        finally:
            if timeout_s:
                self.sock.settimeout(old)

    def read_block(self):
        """Read one IEEE-488.2 definite-length block; returns the payload."""
        self._need(2)
        if self._buf[0:1] != b"#":
            raise TekError("expected a definite-length block, got %r"
                           % bytes(self._buf[:32]))
        ndigits = int(chr(self._buf[1]))
        self._need(2 + ndigits)
        length = int(bytes(self._buf[2:2 + ndigits]).decode("ascii"))
        self._need(2 + ndigits + length)
        payload = bytes(self._buf[2 + ndigits:2 + ndigits + length])
        del self._buf[:2 + ndigits + length]
        if self._buf[:1] == b"\n":          # blocks are usually newline-terminated
            del self._buf[:1]
        return payload

    def query_block(self, command, timeout_s=None):
        if self.sock is None:
            raise TekError("session is not open (call open() first)")
        old = self.sock.gettimeout()
        if timeout_s:
            self.sock.settimeout(float(timeout_s))
        try:
            self.write(command)
            return self.read_block()
        finally:
            if timeout_s:
                self.sock.settimeout(old)

    def query_binary_values(self, command, datatype="h", big_endian=True,
                            container=list, **kwargs):
        payload = self.query_block(command)
        size = struct.calcsize(datatype)
        count = len(payload) // size
        values = struct.unpack((">" if big_endian else "<") + str(count) + datatype,
                               payload[:count * size])
        return container(values)

    def clear(self):
        self.write("*CLS")

    def check_storage(self, bytes_required, directory=None):
        """The PC writes the HDF5 locally, so this is a plain disk-space check."""
        import shutil
        target = Path(directory) if directory else Path.cwd()
        free = shutil.disk_usage(target).free
        if free < bytes_required:
            raise OSError("Insufficient free disk space: need %d, free %d"
                          % (bytes_required, free))
        return True

    def terminate(self):
        self.close()


class Tek:
    """MSO58B bench backend. Mirrors the Mxo surface used by bench.py."""

    backend = "tek"

    def __init__(self, config, session=None):
        self.config = config
        self.session = session or TekSocketSession(
            config["instrument"]["resource"],
            timeout_s=config["instrument"].get("query_timeout_ms", 5000) / 1000.0)
        self.opened = False
        self.settings = None
        self.acquired = False
        self.acquisition_stamp = None

    # ------------------------------------------------------------------ basics
    def open(self):
        if not self.opened:
            self.session.open()
            self.opened = True
        return self

    def query(self, command, **kwargs):
        # Lazily open: scope_api's raw/state commands query the instrument without
        # ever calling prepare(), and Mxo opens in its constructor by contrast.
        self.open()
        return self.session.query(command, **kwargs)

    def write(self, command):
        self.open()
        return self.session.write(command)

    def _identity(self):
        fields = [f.strip() for f in self.query("*IDN?").split(",")]
        if len(fields) < 4:
            raise ValueError("Unexpected *IDN? response: %r" % fields)
        return dict(vendor=fields[0], model=fields[1], serial=fields[2],
                    firmware=fields[3], raw=",".join(fields))

    def identity(self):
        """Validated *IDN? string -- same contract as Mxo.identity().

        Returns the raw identification string and raises if the instrument is not
        the one the config declares.
        """
        ident = self.verify_identity()
        return ident["raw"]

    def verify_identity(self):
        expected = self.config["instrument"]
        ident = self._identity()
        if ident["model"].lower() != str(expected["expected_model"]).lower():
            raise ValueError("Model mismatch: expected %r, instrument reports %r"
                             % (expected["expected_model"], ident["model"]))
        want_serial = str(expected.get("expected_serial") or "")
        if want_serial and ident["serial"].lower() != want_serial.lower():
            raise ValueError("Serial mismatch: expected %r, instrument reports %r"
                             % (want_serial, ident["serial"]))
        return ident

    def errors(self):
        """Raise if the instrument event queue holds anything but 'no events'."""
        raw = self.query("ALLEv?")
        events = []
        for part in raw.split('",'):
            part = part.strip().strip('"')
            if not part:
                continue
            code, _, text = part.partition(",")
            code, text = code.strip(), text.strip().strip('"')
            if code in ("0", "1") and "No events" in text:
                continue
            events.append((code, text))
        if events:
            raise RuntimeError("Instrument reported errors: %s" % events[:6])
        return events

    def link_report(self):
        stamp = None
        try:
            stamp = self.query("ACQuire:NUMACq?")
        except Exception:                                        # noqa: BLE001
            pass
        return dict(backend="tek", acquisitions=stamp, **self.session.connection_info)

    def snapshot(self):
        self.open()
        ident = self._identity()
        queries = {}
        for key, command in (
                ("acquire_state", "ACQuire:STATE?"), ("acquire_mode", "ACQuire:MODe?"),
                ("acquisitions", "ACQuire:NUMACq?"),
                ("stop_after", "ACQuire:STOPAfter?"), ("averages", "ACQuire:NUMAVg?"),
                ("horizontal_mode", "HORizontal:MODe?"),
                ("record_length", "HORizontal:RECOrdlength?"),
                ("seconds_per_div", "HORizontal:SCAle?"),
                ("sample_rate_hz", "HORizontal:SAMPLERate?"),
                ("trigger_mode", "TRIGger:A:MODe?"),
                ("trigger_source", "TRIGger:A:EDGE:SOUrce?"),
                ("trigger_state", "TRIGger:STATE?"),
                ("data_source", "DATa:SOUrce?"), ("data_width", "DATa:WIDth?")):
            try:
                queries[key] = self.query(command)
            except Exception as exc:                             # noqa: BLE001
                queries[key] = "ERR: %s" % str(exc)[:60]
        channels = {}
        for i in range(1, 9):
            row = {}
            for key, command in (("scale", "CH%d:SCAle?"), ("offset", "CH%d:OFFSet?"),
                                 ("coupling", "CH%d:COUPling?"),
                                 ("termination", "CH%d:TERmination?"),
                                 ("bandwidth_hz", "CH%d:BANdwidth?"),
                                 ("probe_gain", "CH%d:PROBe:GAIN?")):
                try:
                    row[key] = self.query(command % i)
                except Exception:                                # noqa: BLE001
                    row[key] = "ERR"
            channels["CH%d" % i] = row
        return dict(idn=ident, queries=queries, channels=channels,
                    link=self.link_report())

    # --------------------------------------------------------------- preamble
    PREAMBLE_FIELDS = (("byt_nr", "WFMOutpre:BYT_Nr?"), ("nr_pt", "WFMOutpre:NR_Pt?"),
                       ("xincr", "WFMOutpre:XINCR?"), ("xzero", "WFMOutpre:XZEro?"),
                       ("pt_off", "WFMOutpre:PT_Off?"), ("ymult", "WFMOutpre:YMUlt?"),
                       ("yoff", "WFMOutpre:YOFf?"), ("yzero", "WFMOutpre:YZEro?"),
                       ("yunit", "WFMOutpre:YUNit?"), ("xunit", "WFMOutpre:XUNit?"))

    def _preamble(self):
        pre = {}
        for key, command in self.PREAMBLE_FIELDS:
            pre[key] = self.query(command).strip()
        try:
            pre["raw"] = self.query("WFMOutpre?")
        except Exception:                                        # noqa: BLE001
            pre["raw"] = ""
        return pre

    # ---------------------------------------------------------------- prepare
    def _set_record_length(self, points, settle_s=0.5, attempts=4):
        if self.query("HORizontal:MODe?").strip().upper() != "MANUAL":
            self.write("HORizontal:MODe MANual")
            time.sleep(settle_s)
        want = int(points)
        last = None
        for _ in range(attempts):
            self.write("HORizontal:RECOrdlength %d" % want)
            time.sleep(settle_s)
            last = self.query("HORizontal:RECOrdlength?").strip()
            if last == str(want):
                return want
        raise ValueError("Instrument did not accept record length %d (reports %s). "
                         "Allowed values are discrete; see Tek.RECORD_LENGTHS."
                         % (want, last))

    def prepare(self, directory):
        directory = Path(directory)
        self.open()
        self.write("*CLS")
        ident = self.verify_identity()
        cfg = self.config

        self.write("ACQuire:STATE OFF")
        self.write("ACQuire:MODe SAMple")
        self.write("ACQuire:STOPAfter SEQuence")
        self.write("ACQuire:NUMAVg 1")
        self.write("TRIGger:A:MODe AUTO")
        self.write("HORizontal:POSition 0")

        duration = float(cfg["timing"]["record_duration_s"])
        # On this instrument the record WINDOW is driven by the timebase and the
        # sample rate follows from it:
        #   * HORizontal:MODe AUTO  -> the scope derives record length AND sample
        #     rate from the requested s/div, and the window is 10 divisions.
        #     Measured: 0.05 s/div -> 0.4 s @ 3.125 MSa/s; 0.1 s/div -> 1 s.
        #   * HORizontal:MODe MANual -> the sample rate stays pinned at the maximum
        #     (6.25 GSa/s), so HORizontal:SCAle writes are silently ignored and the
        #     longest possible window is N/fs = 10 ms at the maximum 62.5 M points.
        #     You cannot reach a 0.5 s window this way.
        # AUTO is therefore the only mode that can honour timing.record_duration_s.
        self.write("HORizontal:MODe AUTO")
        time.sleep(0.6)
        self.write("HORizontal:SCAle %.12g" % (duration / 10.0))
        time.sleep(1.2)

        actual_sdiv = float(self.query("HORizontal:SCAle?"))
        actual_n = int(float(self.query("HORizontal:RECOrdlength?")))
        sample_rate = float(self.query("HORizontal:SAMPLERate?"))
        dt = 1.0 / sample_rate
        span = actual_n * dt

        # The record window and the timebase must describe the same acquisition.
        if abs(span - 10.0 * actual_sdiv) > 3 * dt:
            raise ValueError("Record window and timebase disagree: %d pts @ %.6g s = "
                             "%.6g s, but 10 div x %.6g s/div = %.6g s"
                             % (actual_n, dt, span, actual_sdiv, 10.0 * actual_sdiv))
        # s/div quantises to 1-2-4 style steps (0.05 -> 0.04), so accept the nearest
        # achievable window but refuse anything grossly wrong.
        if not duration / 3.0 <= span <= duration * 3.0:
            raise ValueError("Instrument did not accept the requested record duration: "
                             "wanted %.6g s, got %.6g s (%d pts @ %.6g s/div, %.6g Sa/s)"
                             % (duration, span, actual_n, actual_sdiv, sample_rate))
        want_rate = cfg["acquisition"].get("sample_rate_hz")
        if want_rate and abs(sample_rate - float(want_rate)) > 0.05 * float(want_rate):
            raise ValueError(
                "acquisition.sample_rate_hz=%.6g cannot be honoured on the MSO58B: the "
                "sample rate follows from the timebase (got %.6g Sa/s). Set "
                "acquisition.sample_rate_hz to null and pick timing.record_duration_s "
                "instead." % (float(want_rate), sample_rate))
        selected = selected_channels(cfg)
        if len(selected) * actual_n > cfg["acquisition"]["max_samples_per_channel"] * len(selected):
            raise ValueError("Record exceeds acquisition.max_samples_per_channel.")
        size = actual_n * 4 * len(selected)
        if size > cfg["acquisition"]["max_raw_bytes"]:
            raise ValueError("Record exceeds acquisition.max_raw_bytes.")
        self.session.check_storage(size + 256 * 1024 * 1024, directory)

        channels = {}
        for name in [f"CH{i}" for i in range(1, 9)]:
            i = int(name[2:])
            enabled = bool(cfg["channels"][name].get("enabled"))
            # The matching query is unsupported on this firmware, so this is
            # write-only: we can set the display state but not read it back.
            try:
                self.write("CH%d:DISplay %s" % (i, "ON" if enabled else "OFF"))
            except Exception:                                    # noqa: BLE001
                pass
            row = {"enabled": enabled,
                   "coupling": self.query("CH%d:COUPling?" % i),
                   "scale": self.query("CH%d:SCAle?" % i),
                   "offset": self.query("CH%d:OFFSet?" % i),
                   "termination": self.query("CH%d:TERmination?" % i),
                   "bandwidth_hz": self.query("CH%d:BANdwidth?" % i),
                   "probe_gain": self.query("CH%d:PROBe:GAIN?" % i)}
            channels[name] = row
        self.errors()

        self.settings = dict(
            identity=ident,
            acquisition=dict(record_length=actual_n, resolution_s=dt,
                             sample_rate_hz=sample_rate, duration_s=span,
                             requested_duration_s=duration,
                             seconds_per_div=actual_sdiv,
                             mode=self.query("HORizontal:MODe?"),
                             acquire_mode=self.query("ACQuire:MODe?")),
            timebase=dict(seconds_per_div=self.query("HORizontal:SCAle?")),
            trigger=dict(mode=self.query("TRIGger:A:MODe?"),
                         source=self.query("TRIGger:A:EDGE:SOUrce?")),
            channels=channels)
        atomic_json(directory / "settings.json", self.settings)
        return self.settings

    # ---------------------------------------------------------------- acquire
    def acquire(self):
        self.open()
        self.acquired = False
        timeout_s = float(self.config["instrument"]["acquisition_timeout_s"])
        self.write("ACQuire:STATE ON")
        deadline = time.monotonic() + max(timeout_s, 2.0)
        while time.monotonic() < deadline:
            if self.query("ACQuire:STATE?").strip() == "0":
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Single acquisition did not complete within %.1f s"
                               % timeout_s)
        self.acquisition_stamp = self.query("ACQuire:NUMACq?")
        self.errors()
        self.acquired = True
        return self.acquisition_stamp

    # ----------------------------------------------------------------- export
    def _channel_scale_to_config_unit(self, name, volts, yunit):
        """Convert the measured quantity into the unit the config declares.

        The instrument always reports volts at the input. The config declares the
        physical unit (`A`) plus the probe transfer (`probe_V_per_A`), so the
        conversion is applied here, in software -- this is the one place where the
        Tek path differs semantically from the MXO path, which set the unit on the
        instrument itself and therefore received amperes directly.
        """
        channel = self.config["channels"][name]
        unit = channel.get("unit") or (yunit or "V")
        v_per_a = channel.get("probe_V_per_A")
        if unit == "A" and v_per_a:
            return volts / float(v_per_a), "A", float(v_per_a)
        return volts, unit, None

    def export(self, directory, case):
        if not self.acquired:
            raise RuntimeError("No completed acquisition belonging to this worker.")
        directory = Path(directory)
        final = directory / "waveforms.h5"
        if final.exists():
            raise FileExistsError(final)
        partial = directory / "waveforms.h5.partial"
        prepared_n = int(self.settings["acquisition"]["record_length"])

        if self.query("ACQuire:NUMACq?") != self.acquisition_stamp:
            raise RuntimeError("Acquisition changed before export")

        try:
            import h5py
        except ImportError:                                      # pragma: no cover
            raise RuntimeError("h5py is required to write waveforms.h5")

        axis = None
        with h5py.File(partial, "w") as handle:
            handle.attrs["acquisition_stamp"] = str(self.acquisition_stamp)
            handle.attrs["config_json"] = json.dumps(self.config)
            handle.attrs["case_json"] = json.dumps(case)
            handle.attrs["instrument_json"] = json.dumps(self.settings)
            handle.attrs["backend"] = "tek"
            for name in selected_channels(self.config):
                i = int(name[2:])
                self.write("DATa:SOUrce %s" % name)
                payload = self.query("WFMOutpre:BYT_Nr?")
                width = int(payload)
                if self.query("DATa:ENCdg?").strip().upper() != "RIBINARY":
                    self.write("DATa:ENCdg RIBinary")
                self.write("DATa:WIDth %d" % width)
                self.write("DATa:STARt 1")
                self.write("DATa:STOP %d" % prepared_n)
                pre = self._preamble()
                n = int(float(pre["nr_pt"]))
                dt = float(pre["xincr"])
                t0 = float(pre["xzero"]) - float(pre["pt_off"]) * dt
                if n != prepared_n:
                    raise ValueError("%s: instrument reports %d points, prepared %d"
                                     % (name, n, prepared_n))
                if abs(dt - self.settings["acquisition"]["resolution_s"]) > dt * 1e-6:
                    raise ValueError("%s: sample interval changed during export" % name)

                expected_bytes = n * width
                if expected_bytes > self.config["acquisition"]["max_raw_bytes"]:
                    raise ValueError("%s: %d bytes exceed acquisition.max_raw_bytes"
                                     % (name, expected_bytes))
                self.session.expected_bytes = expected_bytes
                budget = float(self.config["instrument"].get("query_timeout_ms", 5000)) / 1000.0
                budget = max(budget, 5.0, expected_bytes / float(
                    self.config["instrument"].get("min_throughput_bytes_per_s", 200000)))
                datatype = "h" if width == 2 else "b"
                raw = self.session.query_binary_values(
                    "CURVe?", datatype=datatype, big_endian=True,
                    container=np.array, timeout_s=budget)
                if raw.size != n:
                    raise ValueError("%s: expected %d samples, got %d" % (name, n, raw.size))

                ymult = float(pre["ymult"]); yoff = float(pre["yoff"])
                yzero = float(pre["yzero"])
                volts = (raw.astype(np.float64) - yoff) * ymult + yzero
                values, unit, factor = self._channel_scale_to_config_unit(
                    name, volts, pre["yunit"].strip('"'))
                samples = np.asarray(values, dtype="<f4")
                if not np.all(np.isfinite(samples)):
                    raise ValueError("%s: non-finite waveform" % name)

                axis_now = (n, t0)
                if axis is not None and (axis[0] != axis_now[0]
                                         or abs(axis[1] - axis_now[1]) > dt * 0.01):
                    raise ValueError("Channels do not share the same acquisition time axis.")
                axis = axis_now

                group = handle.create_group("channels/" + name)
                group.create_dataset("samples", data=samples, chunks=True, fletcher32=True)
                group.attrs["N"] = n
                group.attrs["t0_s"] = t0
                group.attrs["xstop_s"] = t0 + (n - 1) * dt
                group.attrs["dt_s"] = dt
                group.attrs["raw_header"] = pre.get("raw", "")
                group.attrs["unit"] = unit
                group.attrs["signal"] = self.config["channels"][name]["signal"]
                group.attrs["instrument_yunit"] = pre["yunit"].strip('"')
                if factor:
                    group.attrs["volts_per_unit"] = factor
            self.errors()
            if self.query("ACQuire:NUMACq?") != self.acquisition_stamp:
                raise RuntimeError("Acquisition changed during export")
            handle.flush()

        with h5py.File(partial, "r") as handle:
            for name in selected_channels(self.config):
                group = handle["channels/" + name]
                if group["samples"].shape != (group.attrs["N"],):
                    raise ValueError("Stored waveform shape mismatch for %s" % name)
                block = group["samples"][:]
                if not np.all(np.isfinite(block)):
                    raise ValueError("Stored waveform validation failed for %s" % name)

        digest = sha256(partial)
        partial.replace(final)
        atomic_json(directory / "manifest.json",
                    dict(capture_status="complete", analysis_status="pending",
                         backend="tek",
                         channels=selected_channels(self.config), file=final.name,
                         sha256=digest, metrics=None, link=self.link_report(),
                         connection=getattr(self.session, "connection_info",
                                            {"transport": "tek_socket"})))
        return final

    def close(self, stop=False):
        try:
            if stop and self.opened:
                try:
                    self.write("ACQuire:STATE OFF")
                except Exception:                                # noqa: BLE001
                    pass
        finally:
            try:
                self.session.close()
            finally:
                self.opened = False
