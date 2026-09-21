# Directory layout and sync boundary

The production root is `F:\ZYS_HYBASE\automation`; the Mac mirror is
`02-Engineering/controldesk_automation`.

Source directories are `core`, `config`, `test_cases`, `scope_bench`, `launchers`, `matlab`,
`tools`, `tests`, and `docs`. The root contains only the README and the two normal operator entry
commands.

Runtime and historical directories remain separate and are never moved or deleted by deployment:

- `scope_results/`, `sweep_results/`: measurement output.
- `backup_*/`: deployment rollback copies.
- `*_validation_*/`, `pi_validation_*/`, `validation/`: validation artifacts.
- `parameter_snapshots/`: ControlDesk inventories.
- `scope_bench/sessions/`: GUI session state and stop markers.
- `scope_bench/.venv/`, `__pycache__/`: machine-local generated dependencies.

Deployment uses SHA-256 checks for source files. A directory migration is performed only while no
sweep or scope worker is running, and a complete pre-migration backup is retained on work-pc.
