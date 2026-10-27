$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
& (Join-Path $root 'core\run_parameter_sweep.ps1') -Mode Run -LowVoltage 120 `
    -CasesPath (Join-Path $PSScriptRoot 'cases.csv') `
    -GroupsPath (Join-Path $root 'config\waveform_groups.json') `
    -ObservationsPath (Join-Path $PSScriptRoot 'observation_signals.json') `
    -ScopeConfig (Join-Path $PSScriptRoot 'bench_config.json')
