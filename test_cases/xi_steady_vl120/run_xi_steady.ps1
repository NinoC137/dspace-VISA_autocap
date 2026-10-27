[CmdletBinding()]
param(
    [ValidateSet('Plan','Check','Run')][string]$Mode = 'Plan',
    [ValidateSet(0,100,120,140,160)][int]$LowVoltage = 0,
    [string]$CasesPath = '', [string]$GroupsPath = '', [string]$ObservationsPath = '', [string]$OutputDirectory = '',
    [string]$ScopeConfig = '', [string]$PythonExecutable = '', [string]$StopFile = '',
    [double]$Duration = 0
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $CasesPath) { $CasesPath = Join-Path $PSScriptRoot 'cases.csv' }
if (-not $GroupsPath) { $GroupsPath = Join-Path $root 'config\waveform_groups.json' }
if (-not $ObservationsPath) { $ObservationsPath = Join-Path $PSScriptRoot 'observation_signals.json' }
if (-not $ScopeConfig) { $ScopeConfig = Join-Path $PSScriptRoot 'bench_config.json' }
$runner = Join-Path $root 'test_cases\xi_switch_vl120\run_xi_switch.ps1'
& $runner -Campaign Steady -Mode $Mode -LowVoltage $LowVoltage -CasesPath $CasesPath `
    -GroupsPath $GroupsPath -ObservationsPath $ObservationsPath -OutputDirectory $OutputDirectory `
    -ScopeConfig $ScopeConfig -PythonExecutable $PythonExecutable -StopFile $StopFile -Duration $Duration
