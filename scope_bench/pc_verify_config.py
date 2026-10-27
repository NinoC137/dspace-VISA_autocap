#!/usr/bin/env python3
"""Verify the switched bench_config.json against the bench's own loader/validator."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common                                                  # noqa: E402

cfg = common.load_config(os.path.join(HERE, "bench_config.json"))
i = cfg["instrument"]
print("schema 校验      : 通过 (common.load_config 没抛异常)")
print("resource         :", i["resource"])
print("expected_model   :", i["expected_model"])
print("expected_serial  :", i["expected_serial"])
print("transport        :", i["transport"])
print("remote.host      :", i["remote"]["host"])
print("remote.agent     :", i["remote"]["agent"])
print("channels 保留    :", ",".join(sorted(cfg["channels"])))
print("analysis.channel :", cfg["analysis"]["channel"])
print("timing.settle_s  :", cfg["timing"]["settle_s"])
print("screenshots      :", cfg["screenshots"]["enabled"])
print("selected_channels:", ",".join(common.selected_channels(cfg)))
