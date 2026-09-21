# PACC ControlDesk / MXO38 automation

Production path: `F:\ZYS_HYBASE\automation`.

## Operator entry

Run `SETUP_SCOPE_BENCH.cmd` once when dependencies are missing. For normal use, open
`START_SCOPE_BENCH.cmd`, select one or more test cases, check the connection, then start the
selected batch. A real run writes ControlDesk parameters and `Enable1`; it must be started in the
same interactive Windows desktop session as ControlDesk.

Multiple selected tests run in list order. One automatic batch may contain only tests with the
same `manual_setup`, because the scripts do not change the external LV supply. For example,
`phase_angle_validation_01` and `legacy_vl120` may run together; a VL=120 test and a VL=140 test
must be separate batches.

Command-line equivalents:

```powershell
.\core\run_test_batch.ps1 -Mode List
.\core\run_test_batch.ps1 -Mode Plan -TestIds phase_angle_validation_01,legacy_vl120
.\core\run_test_batch.ps1 -Mode Check -TestIds phase_angle_validation_01
.\core\run_test_batch.ps1 -Mode Run -TestIds phase_angle_validation_01
```

## Directory map

| Directory | Contents |
| --- | --- |
| `core/` | Batch selection, ControlDesk sweep scheduling and safe cleanup |
| `config/` | Shared legacy case table and ControlDesk waveform groups |
| `test_cases/` | Catalog plus self-contained test definitions |
| `scope_bench/` | MXO38 acquisition, Pi transport, analysis and GUI |
| `launchers/legacy/` | Compatibility launchers for the old per-VL workflow |
| `matlab/` | `DspaceRig` and MATLAB hardware examples |
| `tools/` | Read-only inventories, diagnostics and Pi utilities |
| `tests/` | Non-hardware regression tests |
| `docs/` | Operating manuals, handoff notes and reference output |
| `scope_results/`, `sweep_results/` | Runtime output; never reorganized during a run |

The test catalog is `test_cases/catalog.json`. Each entry declares its manual setup, case table,
scope profile, optional observations and expected case count. The expected count is verified before
execution so a stale or accidentally edited CSV cannot silently change batch scope.

Detailed procedures are in `docs/SWEEP_README.md`, `scope_bench/README.md`, and each test case's
own `README.md`.
