"""Offline tests; no live Tek session is constructed."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scope_worker as worker


REFERENCE = worker.read_json(worker.HERE / 'scope_reference.json')


class FakeTek(worker.ReferenceTek):
    def __init__(self, fault=''):
        self.values = copy.deepcopy(REFERENCE['commands'])
        self.values['CH1:SCALE'] = '20'
        self.before = copy.deepcopy(self.values)
        self.writes = []
        self.fault = fault

    def verify_identity(self):
        if self.fault == 'identity':
            raise ValueError('Wrong identity')
        return {}

    def write(self, command):
        self.writes.append(command)
        key, _, value = command.partition(' ')
        if key in self.values:
            if self.fault != 'readback' or key != 'CH1:SCALE':
                self.values[key] = value

    def query(self, command, **kwargs):
        return self.values[command[:-1]]

    def errors(self):
        pass


def fake_base_prepare(scope, directory):
    scope.settings = {'acquisition': dict(sample_rate_hz=125000, record_length=1250000, duration_s=10)}
    if scope.fault == 'geometry':
        scope.settings['acquisition']['sample_rate_hz'] = 312500


class ReferenceTests(unittest.TestCase):
    def test_success_and_reference_provenance(self):
        scope = FakeTek()
        with tempfile.TemporaryDirectory() as directory, patch.object(worker.Tek, 'prepare', fake_base_prepare):
            scope.prepare(directory)
            self.assertEqual(scope.values, REFERENCE['commands'])
            self.assertEqual(len(REFERENCE['commands']), 88)
            saved = worker.read_json(Path(directory) / 'settings.json')
            self.assertEqual(saved['reference']['source_tss_sha256'], REFERENCE['source_tss_sha256'])

    def test_identity_precedes_any_write(self):
        scope = FakeTek('identity')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Wrong identity'):
                scope.prepare(directory)
            self.assertEqual(scope.writes, [])

    def test_readback_failure_restores_channels(self):
        scope = FakeTek('readback')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'not accepted'):
                scope.prepare(directory)
            self.assertEqual(scope.values, scope.before)
            self.assertEqual(worker.read_json(Path(directory) / 'channel_restore.json')['errors'], [])

    def test_geometry_failure_restores_channels(self):
        scope = FakeTek('geometry')
        with tempfile.TemporaryDirectory() as directory, patch.object(worker.Tek, 'prepare', fake_base_prepare):
            with self.assertRaisesRegex(ValueError, '125 kSa/s'):
                scope.prepare(directory)
            self.assertEqual(scope.values, scope.before)

    def test_geometry_rejects_wrong_length_and_duration(self):
        valid = dict(sample_rate_hz=125000, record_length=1250000, duration_s=10)
        for key, wrong in [('sample_rate_hz', 100000), ('record_length', 1000000), ('duration_s', 8)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                worker.verify_geometry({'acquisition': dict(valid, **{key: wrong})}, REFERENCE)


if __name__ == '__main__':
    unittest.main()
