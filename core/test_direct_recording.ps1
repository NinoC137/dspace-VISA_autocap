# Offline fake objects only. Never connects to ControlDesk or instruments.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'direct_recording.ps1')
function New-FakeSignals {
    $signals = [pscustomobject]@{Values=@{}; Added=0}
    $signals | Add-Member ScriptMethod Contains { param($key) return $this.Values.ContainsKey([string]$key) }
    $signals | Add-Member ScriptMethod Item {
        param($key)
        if (-not $this.Values.ContainsKey([string]$key)) { throw ('Missing: ' + $key) }
        return $this.Values[[string]$key]
    }
    $signals | Add-Member ScriptMethod Add {
        param($key)
        $this.Added++
        $this.Values[[string]$key] = [pscustomobject]@{Active=$true;IsReadable=$true}
        return $this.Values[[string]$key]
    }
    return $signals
}
$unit = Join-Path (Split-Path -Parent $PSScriptRoot) 'test_cases\phase_sweep_5deg_vl120'
$groups = Get-Content (Join-Path $unit 'waveform_groups.json') -Raw | ConvertFrom-Json
$direct = @($groups | Where-Object { Test-SweepDirectGroup $_ })
if ($direct.Count -ne 1 -or $direct[0].Signals.Count -ne 6) { throw 'Expected six telemetry output definitions.' }
$variables = New-FakeSignals
foreach ($definition in $direct[0].Signals) {
    $path = $definition -replace '^Platform\(HostService\)://',''
    [void]$variables.Add($path)
}
$description = [pscustomobject]@{Variables=$variables}
Test-SweepDirectSignals $description $direct[0]
$signals = New-FakeSignals
$signals.Values['existing-user-signal'] = [pscustomobject]@{Active=$false;IsReadable=$true}
$mdm = [pscustomobject]@{IsMeasuring=$false;MeasurementConfiguration=[pscustomobject]@{Signals=$signals}}
Register-SweepDirectSignals $mdm $groups
if ($signals.Added -ne 6 -or -not $signals.Values.ContainsKey('existing-user-signal') -or
    $signals.Values['existing-user-signal'].Active) { throw 'Registration changed unrelated user signals.' }
Register-SweepDirectSignals $mdm $groups
if ($signals.Added -ne 6) { throw 'Registration was not idempotent.' }

$unconfigured = New-FakeSignals
$running = [pscustomobject]@{IsMeasuring=$true;MeasurementConfiguration=[pscustomobject]@{Signals=$unconfigured}}
$blocked = $false
try { Register-SweepDirectSignals $running $groups }
catch { $blocked = $_.Exception.Message -match 'Stop the existing ControlDesk measurement first' }
if (-not $blocked -or $unconfigured.Added -ne 0) { throw 'Running measurement was modified.' }

$path = $direct[0].Signals[0] -replace '^Platform\(HostService\)://',''
$variables.Values.Remove($path)
$blocked = $false
try { Test-SweepDirectSignals $description $direct[0] }
catch { $blocked = $_.Exception.Message -match 'Required telemetry missing' }
if (-not $blocked) { throw 'Missing model output did not block preflight.' }
Write-Output 'DIRECT RECORDING OFFLINE OK: six outputs, missing-path guard, idempotent registration, existing signals retained, active measurement protected.'
