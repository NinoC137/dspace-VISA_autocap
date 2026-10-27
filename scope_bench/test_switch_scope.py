#!/usr/bin/env python3
"""switch_scope.py 的离线测试（不连仪器、不动真配置）。

用临时目录里的假 bench_config.json 驱动，验证：
  * 别名解析
  * instrument 段是"逐键合并"，其它段一个字都不改
  * remote 段也逐键合并
  * diff 能正确列出变化
  * --dry-run 绝不落盘
  * 正常切换会写盘并留下备份
  * 内置档案表结构完整、Tek 档案指向正确的资源

跑法: python scope_bench\\test_switch_scope.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import switch_scope as sw                                            # noqa: E402

PROFILES = os.path.join(HERE, "instrument_profiles.json")


def sample_config():
    """模拟真实 bench_config.json 的形状（含不能被改动的段）。"""
    return {
        "schema_version": 1,
        "instrument": {
            "resource": "USB0::0x0AAD::0x0197::1335.2050k08-101400::INSTR",
            "expected_model": "MXO38",
            "expected_serial": "1335.2050k08/101400",
            "query_timeout_ms": 5000,
            "acquisition_timeout_s": 2.5,
            "export_timeout_s": 600,
            "binary_expect_termination": True,
            "transport": "raspi_ssh",
            "remote": {
                "ssh_executable": "C:\\Program Files\\Git\\usr\\bin\\ssh.exe",
                "host": "root@172.16.102.132",
                "python": "/usr/bin/python3",
                "agent": "/root/mxo_bench/pi_agent.py",
                "share_root": "/mnt/scope_results",
                "local_share_root": "F:\\ZYS_HYBASE\\automation\\scope_results",
            },
            "min_throughput_bytes_per_s": 200000,
            "settle_after_clear_s": 0.6,
            "query_attempts": 2,
            "recover_attempts": 2,
        },
        "timing": {"enable_duration_s": 12.0, "settle_s": 6,
                   "record_duration_s": 0.5, "minimum_off_s": 1},
        "acquisition": {"sample_rate_hz": None, "max_samples_per_channel": 100000000},
        "channels": {"CH1": {"enabled": False, "unit": "A"},
                     "CH2": {"enabled": True, "unit": "A"}},
        "analysis": {"channel": "CH2", "fsw_hz": 10000.0},
        "screenshots": {"enabled": True, "spans_s": [0.2, 0.05]},
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swtest-")
        self.cfg = os.path.join(self.tmp, "bench_config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(sample_config(), f, indent=2)
        self.profiles = sw.read_json(PROFILES)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def read_cfg(self):
        return sw.read_json(self.cfg)


class TestResolve(Base):
    def test_exact_and_aliases(self):
        self.assertEqual(sw.resolve(self.profiles, "mso58b"), "mso58b")
        self.assertEqual(sw.resolve(self.profiles, "TEK"), "mso58b")
        self.assertEqual(sw.resolve(self.profiles, "tektronix"), "mso58b")
        self.assertEqual(sw.resolve(self.profiles, "58b"), "mso58b")
        self.assertEqual(sw.resolve(self.profiles, "mxo38"), "mxo38")
        self.assertEqual(sw.resolve(self.profiles, "rns"), "mxo38")

    def test_unknown_name_exits(self):
        with self.assertRaises(SystemExit):
            sw.resolve(self.profiles, "nosuchscope")


class TestApply(Base):
    def test_only_instrument_section_changes(self):
        prof = self.profiles["profiles"]["mso58b"]
        new = sw.apply_profile(sample_config(), prof)
        old = sample_config()
        for section in ("timing", "acquisition", "channels", "analysis", "screenshots"):
            self.assertEqual(new[section], old[section],
                             "切换不该动 %s 段" % section)
        self.assertEqual(new["schema_version"], old["schema_version"])

    def test_instrument_keys_merged_not_replaced(self):
        prof = self.profiles["profiles"]["mso58b"]
        new = sw.apply_profile(sample_config(), prof)
        inst = new["instrument"]
        # 档案给出的键被覆盖
        self.assertEqual(inst["expected_model"], "MSO58B")
        self.assertEqual(inst["expected_serial"], "C070122")
        self.assertEqual(inst["resource"], "TCPIP::192.168.2.200::4000::SOCKET")
        # 档案没提的键必须原样保留
        self.assertEqual(inst["query_timeout_ms"], 5000)
        self.assertEqual(inst["export_timeout_s"], 600)
        self.assertEqual(inst["min_throughput_bytes_per_s"], 200000)
        self.assertEqual(inst["query_attempts"], 2)

    def test_remote_merged_per_key(self):
        prof = json.loads(json.dumps(self.profiles["profiles"]["mso58b"]))
        prof["instrument"]["remote"] = {"host": "root@10.0.0.9"}
        new = sw.apply_profile(sample_config(), prof)
        remote = new["instrument"]["remote"]
        self.assertEqual(remote["host"], "root@10.0.0.9")
        # 未被覆盖的 remote 键仍在
        self.assertEqual(remote["agent"], "/root/mxo_bench/pi_agent.py")
        self.assertEqual(remote["share_root"], "/mnt/scope_results")

    def test_does_not_mutate_input(self):
        cfg = sample_config()
        before = json.dumps(cfg, sort_keys=True)
        sw.apply_profile(cfg, self.profiles["profiles"]["mso58b"])
        self.assertEqual(json.dumps(cfg, sort_keys=True), before)


class TestDiff(Base):
    def test_diff_lists_changed_keys(self):
        old = sample_config()
        new = sw.apply_profile(old, self.profiles["profiles"]["mso58b"])
        changed = {k for k, _, _ in sw.diff_instrument(old, new)}
        self.assertIn("resource", changed)
        self.assertIn("expected_model", changed)
        self.assertIn("expected_serial", changed)
        self.assertNotIn("query_timeout_ms", changed)

    def test_diff_empty_when_identical(self):
        old = sample_config()
        self.assertEqual(sw.diff_instrument(old, old), [])


class TestUseCommand(Base):
    def _use(self, *args):
        return sw.main(["--profiles", PROFILES, "--config", self.cfg, "use"] + list(args))

    def test_dry_run_writes_nothing(self):
        before = open(self.cfg, "rb").read()
        rc = self._use("tek", "--dry-run", "--no-pi")
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.cfg, "rb").read(), before)
        self.assertFalse([f for f in os.listdir(self.tmp) if ".bak-" in f])

    def test_use_writes_and_backs_up(self):
        rc = self._use("tek", "--no-pi")
        self.assertEqual(rc, 0)
        cfg = self.read_cfg()
        self.assertEqual(cfg["instrument"]["expected_model"], "MSO58B")
        self.assertEqual(cfg["instrument"]["resource"],
                         "TCPIP::192.168.2.200::4000::SOCKET")
        self.assertEqual(cfg["instrument"]["transport"], "tek_socket")
        # 其它段没被动
        self.assertEqual(cfg["analysis"]["channel"], "CH2")
        self.assertEqual(cfg["timing"]["settle_s"], 6)
        baks = [f for f in os.listdir(self.tmp) if ".bak-" in f]
        self.assertEqual(len(baks), 1)
        # 备份里是切换前的内容
        old = sw.read_json(os.path.join(self.tmp, baks[0]))
        self.assertEqual(old["instrument"]["expected_model"], "MXO38")

    def test_second_use_is_idempotent(self):
        self._use("tek", "--no-pi")
        first = open(self.cfg, "rb").read()
        self._use("tek", "--no-pi")
        self.assertEqual(open(self.cfg, "rb").read(), first,
                         "已经切到该档案时不该再改文件")

    def test_json_output(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._use("tek", "--no-pi", "--json")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["profile"], "mso58b")
        self.assertEqual(payload["status"], "ready")
        self.assertIsNone(payload["pi_synced"])
        self.assertTrue(any(c["key"] == "resource" for c in payload["changes"]))

    def test_pi_sync_failure_returns_error_after_local_update(self):
        with patch.object(sw, "sync_pi", return_value=(False, "SSH unavailable")):
            rc = self._use("tek")
        self.assertEqual(rc, 1)
        self.assertEqual(self.read_cfg()["instrument"]["expected_model"], "MSO58B")

    def test_list_and_show(self):
        self.assertEqual(sw.main(["--profiles", PROFILES, "--config", self.cfg, "list"]), 0)
        self.assertEqual(sw.main(["--profiles", PROFILES, "--config", self.cfg, "show"]), 0)


class TestShorthand(Base):
    def test_bare_name_means_use(self):
        """文档写的是 `switch_scope.py tek`，必须能用。"""
        rc = sw.main(["--profiles", PROFILES, "--config", self.cfg, "tek", "--no-pi"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_cfg()["instrument"]["expected_model"], "MSO58B")
        self.assertFalse([f for f in os.listdir(self.tmp) if ".bak-" in f] == [],
                         "简写也要真的落盘")

    def test_normalize_argv_shapes(self):
        self.assertEqual(sw.normalize_argv([]), [])
        self.assertEqual(sw.normalize_argv(["tek"]), ["use", "tek"])
        self.assertEqual(sw.normalize_argv(["use", "tek"]), ["use", "tek"])
        self.assertEqual(sw.normalize_argv(["list"]), ["list"])
        self.assertEqual(sw.normalize_argv(["show"]), ["show"])
        self.assertEqual(sw.normalize_argv(["--config", "x.json", "tek"]),
                         ["--config", "x.json", "use", "tek"])
        self.assertEqual(sw.normalize_argv(["tek", "--dry-run"]),
                         ["use", "tek", "--dry-run"])

    def test_dry_run_shorthand_writes_nothing(self):
        before = open(self.cfg, "rb").read()
        rc = sw.main(["--profiles", PROFILES, "--config", self.cfg,
                      "tek", "--dry-run", "--no-pi"])
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_shorthand_from_real_sys_argv(self):
        """命令行真跑时 main() 收到的是 None —— 这一条正是漏过的路径。"""
        saved = sys.argv
        try:
            sys.argv = ["switch_scope.py", "--profiles", PROFILES,
                        "--config", self.cfg, "tek", "--no-pi"]
            rc = sw.main()
        finally:
            sys.argv = saved
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_cfg()["instrument"]["expected_model"], "MSO58B")


class TestProfilesFile(unittest.TestCase):
    def setUp(self):
        self.profiles = sw.read_json(PROFILES)

    def test_shape(self):
        self.assertIn("profiles", self.profiles)
        self.assertIn("active", self.profiles)
        for name, p in self.profiles["profiles"].items():
            self.assertIn("instrument", p, name)
            inst = p["instrument"]
            for key in ("resource", "expected_model", "expected_serial", "transport"):
                self.assertIn(key, inst, "%s 缺 %s" % (name, key))
            self.assertIn("remote", inst, name)
            self.assertIn("host", inst["remote"], name)
            self.assertIn("pi_name", p, name)
            self.assertIn(p.get("status"), ("ready", "config-only"), name)

    def test_tek_profile_points_at_real_instrument(self):
        tek = self.profiles["profiles"]["mso58b"]["instrument"]
        # PC talks straight to the instrument's socket server (no Pi in the path);
        # only the screenshot step needs the Pi, so remote stays for that.
        self.assertEqual(tek["resource"], "TCPIP::192.168.2.200::4000::SOCKET")
        self.assertEqual(tek["expected_model"], "MSO58B")
        self.assertEqual(tek["expected_serial"], "C070122")
        self.assertEqual(tek["transport"], "tek_socket")
        self.assertIn("remote", tek)

    def test_aliases_are_unique(self):
        seen = {}
        for name, p in self.profiles["profiles"].items():
            for a in p.get("aliases", []):
                self.assertNotIn(a.lower(), seen,
                                 "别名 %s 同时属于 %s 和 %s" % (a, seen.get(a.lower()), name))
                seen[a.lower()] = name


if __name__ == "__main__":
    unittest.main(verbosity=2)
