import copy
import tempfile
import unittest
from pathlib import Path

from common import atomic_json, load_config, validate

HERE = Path(__file__).parent


class ConfigMigrationTests(unittest.TestCase):
    def test_legacy_gui_save_preserves_choices_and_recovers_installed_connection(self):
        cfg = load_config(HERE / "bench_config.json")
        for key in ("transport", "remote", "min_throughput_bytes_per_s",
                    "settle_after_clear_s", "query_attempts", "recover_attempts"):
            cfg["instrument"].pop(key, None)
        cfg["timing"]["enable_duration_s"] = 13
        cfg["channels"]["CH8"]["enabled"] = True
        choices = copy.deepcopy(cfg["channels"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            atomic_json(path, cfg)
            loaded = load_config(path)
        self.assertEqual(loaded["instrument"]["transport"], "raspi_ssh")
        self.assertEqual(loaded["instrument"]["remote"]["host"], "root@172.16.102.132")
        self.assertEqual(loaded["instrument"]["min_throughput_bytes_per_s"], 200000)
        self.assertEqual(loaded["channels"], choices)
        self.assertEqual(loaded["timing"]["enable_duration_s"], 13)

    def test_explicit_backend_and_tuning_are_preserved(self):
        cfg = load_config(HERE / "bench_config.json")
        cfg["instrument"].update(transport="local_visa", min_throughput_bytes_per_s=123456)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"explicit.json"
            atomic_json(path, cfg)
            result = load_config(path)
        self.assertEqual(result["instrument"]["transport"], "local_visa")
        self.assertEqual(result["instrument"]["min_throughput_bytes_per_s"], 123456)
        result["instrument"]["query_attempts"] = 0
        with self.assertRaises(ValueError):
            validate(result)


if __name__ == "__main__":
    unittest.main()
