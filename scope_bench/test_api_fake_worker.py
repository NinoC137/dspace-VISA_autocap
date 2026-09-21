"""Test-only worker entry: the production bench worker wired to a fake instrument.

Used exclusively by test_api.py to exercise scope_api.py end to end (real subprocesses,
real CLI, real file conventions) without touching the oscilloscope. It never connects to
hardware: it constructs Mxo around the in-memory FakeVisa from test_bench.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bench import worker  # noqa: E402
from common import read_json  # noqa: E402
from mxo import Mxo  # noqa: E402
from test_bench import FakeVisa  # noqa: E402


def main():
    directory = Path(sys.argv[1]).resolve()
    config = read_json(directory / "config.json")
    return worker(config, directory, lambda cfg: Mxo(cfg, FakeVisa(cfg)), standalone=True)


if __name__ == "__main__":
    sys.exit(main())
