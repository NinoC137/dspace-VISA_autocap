[CmdletBinding()]
param([switch]$Run, [switch]$Check)
if ($Run -and $Check) { throw 'Choose Run or Check.' }
$mode = if ($Run) { 'Run' } elseif ($Check) { 'Check' } else { 'Plan' }
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
& (Join-Path $root 'core\run_parameter_sweep.ps1') -LowVoltage 100 -Mode $mode
