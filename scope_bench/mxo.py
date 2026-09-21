"""MXO SCPI adapter. All writes occur in explicit prepare/acquire/export calls."""
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import time
import tempfile

import h5py
import numpy as np
from pyvisa.errors import VisaIOError

from common import atomic_json, selected_channels, sha256


class VisaLease:
    """Process-scoped lock shared by check and capture, including separate GUI instances."""
    def __init__(self, resource):
        key = hashlib.sha256(resource.encode()).hexdigest()[:24]
        self.stream = open(Path(tempfile.gettempdir()) / ("mxo_bench_" + key + ".lock"), "a+b")
        try:
            if self.stream.seek(0, 2) == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise RuntimeError("Another bench process is using this VISA resource.")

    def close(self):
        self.stream.close()


def boolean(value):
    value = value.strip().upper()
    if value not in ("0", "1", "OFF", "ON"):
        raise ValueError(f"Invalid boolean response: {value}")
    return value in ("1", "ON")


def parse_header(raw, dt):
    fields = next(csv.reader([raw]))
    if len(fields) != 4:
        raise ValueError(f"Unexpected waveform header: {raw}")
    start, stop, count, values = map(float, fields)
    if not all(math.isfinite(v) for v in (start, stop, count, values, dt)):
        raise ValueError("Non-finite waveform header.")
    if count != int(count) or count < 2 or values != 1 or dt <= 0 or stop <= start:
        raise ValueError("Expected one scalar per sample, a positive interval and at least two samples.")
    # Use the instrument's sample interval; tolerate either documented endpoint convention.
    span_samples = (stop-start)/dt
    error = min(abs(span_samples-count), abs(span_samples-(count-1)))
    if error > max(0.02, count*1e-7):
        raise ValueError("Header span does not match acquisition resolution.")
    return dict(t0_s=start, xstop_s=stop, dt_s=dt, N=int(count), raw_header=raw)


class Mxo:
    def __init__(self, config, session=None):
        self.config = config
        self.manager = None
        self.lease = None
        self.acquired = False
        if session is None:
            self.lease = VisaLease(config["instrument"]["resource"])
            try:
                if config["instrument"].get("transport", "local_visa") == "raspi_ssh":
                    from pi_transport import PiSession
                    session = PiSession(config)
                else:
                    import pyvisa
                    self.manager = pyvisa.ResourceManager()
                    session = self.manager.open_resource(config["instrument"]["resource"])
            except BaseException:
                if self.manager:
                    self.manager.close()
                self.lease.close()
                raise
        self.session = session
        session.timeout = config["instrument"]["query_timeout_ms"]
        session.write_termination = "\n"
        session.read_termination = "\n"
        session.chunk_size = 1024 * 1024
        # This USB link drops operations intermittently; count them so a capture can
        # report how much trouble the instrument gave it.
        self.max_attempts = int(config["instrument"]["query_attempts"])
        self.link_errors = 0
        self.recoveries = 0
        self.operations = 0

    def query(self, command):
        return self.recoverable(f"query {command}", lambda: self._query(command))

    def _query(self, command):
        return self.session.query(command).strip()

    def recoverable(self, context, operation):
        """Run one instrument operation, self-healing a flapping link.

        This USB link drops operations intermittently: a read times out, the instrument
        may still hold the unread response, and every later operation then times out too.
        A VISA clear aborts that transfer and resynchronises the link, so a transient
        fault costs a retry rather than the whole batch. Retries and the failures that
        survived them are counted and reported with the capture.
        """
        last = None
        for attempt in range(1, self.max_attempts + 1):
            self.operations += 1
            try:
                return operation()
            except VisaIOError as exc:
                last = exc
                self.link_errors += 1
                if attempt == self.max_attempts:
                    break
                self.recover(f"{context} (attempt {attempt}/{self.max_attempts})")
                self.recoveries += 1
        raise last

    def link_report(self):
        return dict(link_errors=self.link_errors, recoveries=self.recoveries,
                    operations=self.operations, max_attempts=self.max_attempts)

    def read_payload(self, command, expected_bytes):
        """Read one complete definite-length binary block, or abort before writing it.

        The payload is buffered in full and length-checked before the caller stores it,
        so a truncated transfer can never be mistaken for a measurement. The instrument
        is stopped here, which is what makes re-sending this query safe.
        """
        last = None
        for attempt in range(1, self.max_attempts + 1):
            self.operations += 1
            self.session.timeout = self._transfer_timeout_ms(expected_bytes)
            try:
                if hasattr(self.session, "expected_bytes"):
                    self.session.expected_bytes = expected_bytes
                values = self.session.query_binary_values(
                    command, datatype="f", is_big_endian=False, container=np.array,
                    header_fmt="ieee",
                    expect_termination=self.config["instrument"]["binary_expect_termination"])
            except VisaIOError as exc:
                last = exc
                self.link_errors += 1
                if attempt == self.max_attempts:
                    break
                self.recover(f"{command} ({expected_bytes} bytes, attempt {attempt})")
                self.recoveries += 1
                continue
            if len(values) * 4 != expected_bytes:
                raise ValueError(f"Incomplete transfer for {command}: expected {expected_bytes} bytes, "
                                 f"got {len(values) * 4}. Reduce the record length or raise "
                                 f"instrument.export_timeout_s.")
            return values
        raise RuntimeError(
            f"Waveform transfer of {expected_bytes} bytes failed {self.max_attempts} times for "
            f"{command}. Last error: {last}. The oscilloscope link is unusable; power-cycle the "
            f"MXO38 and check its USB cable and hub.") from last

    def _transfer_timeout_ms(self, expected_bytes):
        """Give the whole block one budget: a truncated transfer is what breaks the link.

        The configured timeout is the floor; above it the budget scales with the payload
        so that a long record is never cut off in the middle of the transfer.
        """
        configured_ms = 1000 * float(self.config["instrument"]["export_timeout_s"])
        floor_bytes_per_s = float(self.config["instrument"]["min_throughput_bytes_per_s"])
        scaled_ms = 2000.0 * expected_bytes / floor_bytes_per_s
        return round(max(configured_ms, scaled_ms))

    def recover(self, context):
        """Clear a stalled transfer and prove the instrument answers again.

        A VISA ``clear`` aborts the pending USBTMC transfer, which is what releases a
        desynchronised link. The instrument is given several attempts with growing
        pauses, because this link sometimes stays silent for a few seconds; only a link
        that stays silent is reported as fatal.
        """
        settle = float(self.config["instrument"]["settle_after_clear_s"])
        attempts = int(self.config["instrument"]["recover_attempts"])
        last = None
        for attempt in range(1, attempts + 1):
            try:
                self.session.clear()
            except Exception:
                pass
            time.sleep(settle * attempt)
            try:
                self.session.timeout = self.config["instrument"]["query_timeout_ms"]
                self._query("*IDN?")
                return
            except Exception as exc:
                last = exc
        raise RuntimeError(
            f"Lost the oscilloscope link during {context} and it did not answer after {attempts} "
            f"clears: {last}. Power-cycle the MXO38 (and check its USB cable and hub) before "
            f"retrying; every case will fail until it responds to *IDN?.") from last

    def identity(self):
        value = self.query("*IDN?")
        fields = next(csv.reader([value]))
        cfg = self.config["instrument"]
        if len(fields) != 4 or fields[1] != cfg["expected_model"] or fields[2] != cfg["expected_serial"]:
            raise ValueError(f"Unexpected scope identity: {value}")
        return value

    def errors(self):
        errors = []
        for _ in range(32):
            response = self.query("SYSTem:ERRor?")
            if int(response.split(",", 1)[0]) == 0:
                break
            errors.append(response)
        else:
            errors.append("error queue did not empty")
        if errors:
            raise RuntimeError("SCPI errors: " + "; ".join(errors))

    def snapshot(self):
        """Best-effort metadata read.

        This instrument drops individual queries, and one unanswered query can silence the
        session. Metadata is only recorded, so a command that does not answer is reported
        as unavailable rather than failing the whole case. The values that decide
        correctness are re-read and checked separately in prepare().
        """
        info = {"idn": self.identity(), "channels": {}, "acquisition": {}, "unavailable": []}
        info["connection"] = getattr(self.session, "connection_info", {"transport": "local_visa"})
        commands = {"resolution_s": "ACQuire:RESolution?", "sample_rate_hz": "ACQuire:SRReal?",
                    "record_length": "ACQuire:RLReal?", "type": "ACQuire:TYPE?",
                    "count": "ACQuire:COUNt?", "time_range_s": "TIMebase:RANGe?",
                    "trigger_mode": "TRIGger:MODE?"}
        for key, command in commands.items():
            try:
                info["acquisition"][key] = self.query(command)
            except Exception:
                info["acquisition"][key] = None
                info["unavailable"].append(command)
        for i in range(1, 9):
            name = f"CH{i}"
            try:
                entry = {"enabled": boolean(self.query(f"CHANnel{i}:STATe?"))}
            except Exception:
                entry = {"enabled": None}
                info["unavailable"].append(f"CHANnel{i}:STATe?")
            if name in selected_channels(self.config):
                queries = {"unit": f"PROBe{i}:SETup:ATTenuation:UNIT?",
                           "scale": f"CHANnel{i}:SCALe?", "offset": f"CHANnel{i}:OFFSet?",
                           "coupling": f"CHANnel{i}:COUPling?", "bandwidth_hz": f"CHANnel{i}:BANDwidth?",
                           "waveform_type": f"CHANnel{i}:WAVeform:TYPE?",
                           "arithmetics": f"CHANnel{i}:WAVeform:ARIThmetics?"}
                for key, command in queries.items():
                    try:
                        entry[key] = self.query(command).strip('"')
                    except Exception:
                        entry[key] = None
                        info["unavailable"].append(command)
                if entry.get("unit") is None:
                    raise RuntimeError(f"{name}: the probe unit could not be read because the "
                                       f"instrument link dropped that query. Retry, and "
                                       f"power-cycle the MXO38 if it keeps failing.")
                unit = entry["unit"].upper()
                expected = self.config["channels"][name]["unit"]
                if unit not in ("A", "V", "W") or (expected is not None and unit != expected):
                    raise ValueError(f"{name}: expected unit {expected}, instrument returned {unit}")
                entry["unit"] = unit
            info["channels"][name] = entry
        self.errors()
        return info

    def prepare(self, directory):
        # Every write here is absolute, so restarting the whole sequence after a link
        # loss leaves the instrument in the same known configuration.
        return self.recoverable("prepare",
                                lambda: self._prepare(Path(directory)))

    def _prepare(self, directory):
        before = self.snapshot()
        atomic_json(directory / "settings_before.json", before)
        s, cfg = self.session, self.config
        s.write("STOP")
        for i in range(1, 9):
            enabled = cfg["channels"][f"CH{i}"]["enabled"]
            s.write(f"CHANnel{i}:STATe {int(enabled)}")
            if enabled:
                s.write(f"CHANnel{i}:WAVeform:TYPE SAMPle")
                s.write(f"CHANnel{i}:WAVeform:ARIThmetics OFF")
        s.write("ACQuire:TYPE SAMPle")
        s.write("ACQuire:COUNt 1")
        s.write("TRIGger:MODE AUTO")
        s.write("TIMebase:REFerence 0")
        s.write("TIMebase:HORizontal:POSition 0")
        if cfg["instrument"].get("transport") == "raspi_ssh":
            # The delivered single-channel script uses MAN points. Restore automatic
            # record length so the requested duration and actual sample rate define N.
            s.write("ACQuire:POINts:MODE AUTO")
        s.write(f"TIMebase:RANGe {cfg['timing']['record_duration_s']:.12g}")
        rate = cfg["acquisition"]["sample_rate_hz"] or float(before["acquisition"]["sample_rate_hz"])
        # Use SRReal? for hardware readback; this scope rejects SRReal writes.
        s.write("ACQuire:SRATe:MODE MANual")
        s.write(f"ACQuire:SRATe:VALue {rate:.12g}")
        s.write("FORMat REAL,32")
        s.write("FORMat:BORDer LSBFirst")
        if self.query("*OPC?") != "1":
            raise RuntimeError("Settings did not complete.")
        info = self.snapshot()
        for name, item in info["channels"].items():
            if item["enabled"] != cfg["channels"][name]["enabled"]:
                raise RuntimeError("Channel state readback mismatch: " + name)
            if item["enabled"] and (not item["waveform_type"].upper().startswith("SAMP") or item["arithmetics"].upper() != "OFF"):
                raise RuntimeError("Unexpected waveform processing: " + name)
        if int(info["acquisition"]["count"]) != 1 or not info["acquisition"]["type"].upper().startswith("SAMP"):
            raise RuntimeError("Single Sample acquisition was not accepted.")
        dt = float(info["acquisition"]["resolution_s"])
        fs = float(info["acquisition"]["sample_rate_hz"])
        n = int(info["acquisition"]["record_length"])
        duration = cfg["timing"]["record_duration_s"]
        if not math.isfinite(dt) or dt <= 0 or not math.isfinite(fs) or fs <= 0 or n < 2:
            raise ValueError("Invalid acquisition settings.")
        if abs(dt*fs-1) > 1e-4:
            raise ValueError("Interpolated waveform: resolution and hardware sample rate differ.")
        if abs(fs/rate-1) > 0.01:
            raise ValueError("Instrument could not maintain the selected sample rate; reduce channels/record length or configure a validated rate.")
        if abs(n*dt-duration) > max(2*dt, duration*0.01):
            raise ValueError("Instrument did not accept the requested record duration.")
        if cfg["analysis"]["channel"] in selected_channels(cfg):
            a = cfg["analysis"]
            target = info["channels"][a["channel"]]
            if target["unit"] != "A" or not target["coupling"].upper().startswith("DC"):
                raise ValueError("Current RMS analysis requires an ampere channel with DC coupling.")
            if fs/2 <= 2*a["fsw_hz"]+(a["q_max"]+0.5)*a["f0_hz"]:
                raise ValueError("Actual sample rate cannot cover the configured carrier groups.")
        size = n * 4 * len(selected_channels(cfg))
        if n > cfg["acquisition"]["max_samples_per_channel"] or size > cfg["acquisition"]["max_raw_bytes"]:
            raise ValueError("Record exceeds configured memory budget; adjust sample rate/record length.")
        spectral_budget = n*20 if cfg["analysis"]["channel"] in selected_channels(cfg) else 0
        if shutil.disk_usage(directory).free < size + spectral_budget + 256*1024*1024:
            raise OSError("Insufficient free disk space for waveform and analysis.")
        if hasattr(s, "check_storage"):
            # The binary spool and final HDF5 coexist until each channel is verified.
            s.check_storage(size + n*4 + spectral_budget + 256*1024*1024)
        if boolean(self.query("ACQuire:POMemory?")):
            raise RuntimeError("Instrument acquisition memory is insufficient.")
        self.errors()
        self.settings = info
        atomic_json(directory / "settings.json", info)

    def acquire(self):
        # RUNSingle is idempotent while the instrument is stopped, so recovery may retry it.
        return self.recoverable("acquire", self._acquire)

    def _acquire(self):
        self.acquired = False
        old_timeout = self.session.timeout
        self.session.timeout = round(1000*self.config["instrument"]["acquisition_timeout_s"])
        try:
            # This blocking operation runs only in the worker process, never the enable timer.
            self.session.write("RUNSingle")
            if self.query("*OPC?") != "1":
                raise RuntimeError("Single acquisition did not complete.")
            self.errors()
            self.acquisition_stamp = self.query("ACQuire:HISTory:ISODate?")
            self.acquired = True
        finally:
            self.session.timeout = old_timeout

    def export(self, directory, case):
        if not self.acquired:
            raise RuntimeError("No completed acquisition belonging to this worker.")
        directory = Path(directory)
        final = directory / "waveforms.h5"
        if final.exists():
            raise FileExistsError(final)
        partial = directory / "waveforms.h5.partial"
        dt = float(self.settings["acquisition"]["resolution_s"])
        axis = None
        if self.query("ACQuire:HISTory:ISODate?") != self.acquisition_stamp:
            raise RuntimeError("Acquisition changed before export")
        with h5py.File(partial, "w") as h:
            h.attrs["acquisition_stamp"] = self.acquisition_stamp
            h.attrs["config_json"] = json.dumps(self.config)
            h.attrs["case_json"] = json.dumps(case)
            h.attrs["instrument_json"] = json.dumps(self.settings)
            for name in selected_channels(self.config):
                i = int(name[2:])
                raw_header = self.query(f"CHANnel{i}:DATA:HEADer?")
                header = parse_header(raw_header, dt)
                prepared_n = int(self.settings["acquisition"]["record_length"])
                # MXO38 firmware 2.8.2 can report one more acquisition point than
                # the exported header. Use the header count for the actual transfer;
                # parse_header still validates its span against the sample interval.
                if abs(header["N"] - prepared_n) > 1:
                    raise ValueError(f"Export length differs from prepared record length: "
                                     f"{name}, prepared={prepared_n}, header={header['N']}, "
                                     f"raw_header={raw_header!r}.")
                if header["t0_s"] < -dt*2:
                    raise ValueError("Pretrigger samples found despite zero-pretrigger request.")
                current_axis = (header["N"], header["t0_s"])
                if axis is not None and (axis[0] != current_axis[0] or abs(axis[1]-current_axis[1]) > dt*0.01):
                    raise ValueError("Channels do not share the same acquisition time axis.")
                axis = current_axis
                if boolean(self.query(f"CHANnel{i}:OVERload:STATus:CONDition?")):
                    raise ValueError(name + ": instrument overload reported")
                expected_bytes = header["N"] * 4
                budget = self._transfer_timeout_ms(expected_bytes)
                if expected_bytes > self.config["acquisition"]["max_raw_bytes"]:
                    raise ValueError(f"{name}: {expected_bytes} bytes exceed acquisition.max_raw_bytes; "
                                     "reduce record_duration_s or the sample rate.")
                self.session.timeout = budget
                y = self.read_payload(f"CHANnel{i}:DATA:VALues?", expected_bytes)
                if len(y) != header["N"] or not np.all(np.isfinite(y)):
                    raise ValueError(name + ": incomplete/non-finite waveform")
                if self.query(f"CHANnel{i}:DATA:HEADer?") != raw_header:
                    raise ValueError("Waveform changed during export.")
                group = h.create_group("channels/" + name)
                group.create_dataset("samples", data=np.asarray(y, dtype="<f4"), chunks=True, fletcher32=True)
                for key, value in header.items():
                    group.attrs[key] = value
                group.attrs["unit"] = self.settings["channels"][name]["unit"]
                group.attrs["signal"] = self.config["channels"][name]["signal"]
            self.errors()
            if self.query("ACQuire:HISTory:ISODate?") != self.acquisition_stamp:
                raise RuntimeError("Acquisition changed during export")
            h.flush()
        with h5py.File(partial, "r") as h:
            for name in selected_channels(self.config):
                g = h["channels/" + name]
                if g["samples"].shape != (g.attrs["N"],):
                    raise ValueError("Stored waveform shape mismatch.")
                for start in range(0, g.attrs["N"], 1000000):
                    if not np.all(np.isfinite(g["samples"][start:start+1000000])):
                        raise ValueError("Stored waveform validation failed.")
        digest = sha256(partial)
        partial.replace(final)
        atomic_json(directory / "manifest.json", dict(capture_status="complete", analysis_status="pending",
                    channels=selected_channels(self.config), file=final.name, sha256=digest, metrics=None,
                    link=self.link_report(),
                    connection=getattr(self.session, "connection_info", {"transport": "local_visa"})))
        return final

    def close(self, stop=False):
        try:
            if stop:
                self.session.write("STOP")
        finally:
            try:
                self.session.close()
            finally:
                if self.manager:
                    self.manager.close()
                if self.lease:
                    self.lease.close()
