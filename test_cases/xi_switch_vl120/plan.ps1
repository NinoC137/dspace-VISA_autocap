$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
& (Join-Path $PSScriptRoot 'run_xi_switch.ps1') -Mode Plan -LowVoltage 120 `
    -CasesPath (Join-Path $PSScriptRoot 'cases.csv') `
    -TrajectoryPath (Join-Path $PSScriptRoot 'trajectory.json') `
    -GroupsPath (Join-Path $root 'config\waveform_groups.json') `
    -ObservationsPath (Join-Path $PSScriptRoot 'observation_signals.json') `
    -ScopeConfig (Join-Path $PSScriptRoot 'bench_config.json') `
    -OutputDirectory (Join-Path $root 'scope_results\sweeps\xi_switch_vl120')
