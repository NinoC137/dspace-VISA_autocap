"""Unit-specific Tek configuration; shared worker retains capture/export ownership.

Import and plan are offline. Instrument calls occur only in prepare/check/run.
"""
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'scope_bench'))

import bench
from common import atomic_json, read_json, sha256
from tek import Tek


def matches(actual, expected):
    actual = str(actual).strip().strip('"')
    expected = str(expected).strip().strip('"')
    try:
        return math.isclose(float(actual), float(expected), rel_tol=1e-5, abs_tol=1e-8)
    except ValueError:
        return actual.casefold() == expected.casefold()


def verify_geometry(settings, reference):
    acquisition = settings['acquisition']
    if (not math.isclose(acquisition['sample_rate_hz'], reference['sample_rate_hz'], rel_tol=1e-6)
            or acquisition['record_length'] != reference['record_length']
            or not math.isclose(acquisition['duration_s'], reference['duration_s'], abs_tol=1e-6)):
        raise ValueError('Reference requires exactly 125 kSa/s, 1,250,000 points and 10 s; Enable1 must remain 0.')


class ReferenceTek(Tek):
    def prepare(self, directory):
        directory = Path(directory)
        reference_path = HERE / 'scope_reference.json'
        reference = read_json(reference_path)
        atomic_json(directory / 'scope_reference.json', reference)
        # Identity is checked before any configuration write.
        self.verify_identity()
        self.write('ACQuire:STATE OFF')
        self.write('*CLS')
        commands = reference['commands']
        before = {key: self.query(key + '?') for key in commands}
        atomic_json(directory / 'channel_settings_before.json', before)
        applied = []
        try:
            for key, value in commands.items():
                # Track attempted writes too: an exception can occur after acceptance.
                applied.append(key)
                self.write(key + ' ' + value)
            self.errors()
            actual = {key: self.query(key + '?') for key in commands}
            atomic_json(directory / 'channel_settings_readback.json', actual)
            mismatches = [key for key in commands if not matches(actual[key], commands[key])]
            if mismatches:
                raise ValueError('Reference channel settings not accepted: ' + ', '.join(mismatches))
            super().prepare(directory)
            verify_geometry(self.settings, reference)
            self.settings['reference'] = dict(
                file='scope_reference.json', sha256=sha256(reference_path),
                source_tss=reference['source_tss'], source_tss_sha256=reference['source_tss_sha256'])
            atomic_json(directory / 'settings.json', self.settings)
        except BaseException:
            restore_errors = []
            # Preserve reference ordering: attenuation/units precede final scale.
            for key in commands:
                if key in applied:
                    try:
                        self.write(key + ' ' + str(before[key]))
                    except Exception as exc:
                        restore_errors.append(key + ': ' + str(exc))
            atomic_json(directory / 'channel_restore.json', dict(errors=restore_errors))
            raise


def make_reference_scope(config, session=None):
    if (config['instrument']['transport'] != 'tek_socket'
            or config['instrument']['expected_model'] != 'MSO58B'
            or config['instrument']['expected_serial'] != 'C070122'):
        raise ValueError('This unit requires MSO58B C070122.')
    return ReferenceTek(config, session=session)


if __name__ == '__main__':
    bench.make_scope = make_reference_scope
    bench.main()
