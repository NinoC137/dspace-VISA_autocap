[CmdletBinding()]
param(
    [ValidateSet('Plan','Check','Run')][string]$Mode = 'Plan',
    [ValidateSet(0,120)][int]$LowVoltage = 120,
    [string]$CasesPath = '', [string]$GroupsPath = '', [string]$ObservationsPath = '',
    [string]$OutputDirectory = '', [string]$ScopeConfig = '',
    [string]$PythonExecutable = '', [string]$StopFile = '', [double]$Duration = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $CasesPath) { $CasesPath = Join-Path $PSScriptRoot 'cases.csv' }
if (-not $GroupsPath) { $GroupsPath = Join-Path $PSScriptRoot 'waveform_groups.json' }
if (-not $ObservationsPath) { $ObservationsPath = Join-Path $PSScriptRoot 'observation_signals.json' }
if (-not $ScopeConfig) { $ScopeConfig = Join-Path $PSScriptRoot 'bench_config.json' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $root 'scope_results\sweeps' }
$rows = @(Import-Csv -LiteralPath $CasesPath | Where-Object enabled -eq 'true')
$expected = @('0.515','0.8' | ForEach-Object {
    $xi = [double]::Parse($_, [Globalization.CultureInfo]::InvariantCulture)
    for ($phi = $(if ($xi -eq 0.515) { 120 } else { 0 }); $phi -le 180; $phi += 5) { '{0}|{1}' -f $xi,$phi }
})
$actual = @($rows | ForEach-Object {
    if ([double]$_.VL_V -ne 120 -or [double]$_.ud_ref6 -ne 60 -or [double]$_.ud_ref7 -ne 60) {
        throw 'This unit requires VL=120 V and ud_ref6=ud_ref7=60.'
    }
    '{0}|{1}' -f ([double]$_.xi),([double]$_.phi_deg)
})
if ($actual.Count -ne 50 -or ($actual -join ',') -ne ($expected -join ',')) {
    throw 'Expected xi=0.515 phi=120..180, then xi=0.8 phi=0..180, in 5-degree steps.'
}
$config = Get-Content -LiteralPath $ScopeConfig -Raw | ConvertFrom-Json
if ($config.instrument.transport -ne 'tek_socket' -or $config.instrument.expected_model -ne 'MSO58B' -or
    $config.instrument.expected_serial -ne 'C070122' -or $config.timing.enable_duration_s -ne 15 -or
    $config.timing.record_duration_s -ne 10 -or $config.timing.settle_s -ne 1 -or
    $config.instrument.acquisition_timeout_s -ne 12 -or $config.acquisition.sample_rate_hz -ne 125000 -or
    @($config.channels.PSObject.Properties | Where-Object { -not $_.Value.enabled }).Count -ne 0 -or
    -not $config.screenshots.enabled) { throw 'Use the fixed 8-channel MSO58B 125 kSa/s, 10 s configuration.' }
& (Join-Path $root 'core\run_parameter_sweep.ps1') -Mode $Mode -LowVoltage 120 `
    -CasesPath $CasesPath -GroupsPath $GroupsPath -ObservationsPath $ObservationsPath `
    -OutputDirectory $OutputDirectory -ScopeConfig $ScopeConfig -PythonExecutable $PythonExecutable `
    -StopFile $StopFile -Duration $Duration -ScopeWorkerScript (Join-Path $PSScriptRoot 'scope_worker.py')
