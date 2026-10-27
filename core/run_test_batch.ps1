[CmdletBinding()]
param(
    [ValidateSet('List','Plan','Check','Run')][string]$Mode = 'List',
    [string]$TestIds = '',
    [string]$CatalogPath = '',
    [string]$OutputDirectory = '',
    [string]$SessionScopeConfig = '',
    [string]$PythonExecutable = '',
    [string]$StopFile = ''
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
Set-StrictMode -Version 2.0

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $CatalogPath) { $CatalogPath = Join-Path $ProjectRoot 'test_cases\catalog.json' }
$CatalogPath = (Resolve-Path -LiteralPath $CatalogPath).Path
$catalog = Get-Content -LiteralPath $CatalogPath -Raw | ConvertFrom-Json
if ($catalog.schema_version -ne 1) { throw 'Unsupported test catalog schema.' }
$allTests = @($catalog.tests | ForEach-Object { $_ })
$byId = @{}
foreach ($test in $allTests) {
    if ($test.id -notmatch '^[a-z][a-z0-9_-]+$' -or $byId.ContainsKey([string]$test.id)) {
        throw ('Invalid or duplicate test id: ' + $test.id)
    }
    $byId[[string]$test.id] = $test
}

if ($Mode -eq 'List') {
    $allTests | Where-Object enabled | Select-Object id,name,manual_setup,low_voltage_v,expected_cases |
        Format-Table -AutoSize | Out-Host
    return
}

$requested = @($TestIds.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $requested.Count) { throw 'Select at least one test id.' }
$seen = @{}
$selected = @()
foreach ($id in $requested) {
    if ($seen.ContainsKey($id)) { throw ('Duplicate selected test id: ' + $id) }
    $seen[$id] = $true
    if (-not $byId.ContainsKey($id) -or -not $byId[$id].enabled) { throw ('Unknown or disabled test id: ' + $id) }
    $selected += $byId[$id]
}
$setups = @($selected | Select-Object -ExpandProperty manual_setup -Unique)
if ($Mode -eq 'Run' -and $setups.Count -ne 1) {
    throw ('One automatic batch cannot cross manual supply setups: ' + ($setups -join ', '))
}
$customTests = @($selected | Where-Object {
    $_.PSObject.Properties['runner'] -and [string]$_.runner
})
if ($customTests.Count -gt 0 -and $selected.Count -ne 1) {
    throw 'A dedicated xi test must be selected alone.'
}

function Resolve-ProjectPath([string]$Value, [bool]$Required = $true) {
    if (-not $Value) {
        if ($Required) { throw 'A required catalog path is empty.' }
        return ''
    }
    $candidate = if ([IO.Path]::IsPathRooted($Value)) { $Value } else { Join-Path $ProjectRoot $Value }
    return (Resolve-Path -LiteralPath $candidate).Path
}

$batchDirectory = ''
$status = [ordered]@{
    StartedAt=(Get-Date).ToString('o'); FinishedAt=$null; Mode=$Mode
    TestIds=@($requested); ManualSetup=@($setups); Tests=@(); Stopped=$false; Complete=$false; Error=''
}
if ($Mode -eq 'Run') {
    if (-not $OutputDirectory) { $OutputDirectory = Join-Path $ProjectRoot 'sweep_results' }
    [void](New-Item -ItemType Directory -Force -Path $OutputDirectory)
    $batchDirectory = Join-Path $OutputDirectory ('batch_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + $PID)
    [void](New-Item -ItemType Directory -Path $batchDirectory)
    Copy-Item -LiteralPath $CatalogPath -Destination (Join-Path $batchDirectory 'catalog.json')
}

try {
    foreach ($test in $selected) {
        $hasCustomRunner = $test.PSObject.Properties['runner'] -and [string]$test.runner
        $cases = Resolve-ProjectPath ([string]$test.cases)
        $groups = Resolve-ProjectPath ([string]$test.groups)
        $observations = Resolve-ProjectPath ([string]$test.observations) $false
        $scopeConfig = if ($hasCustomRunner -and $test.fixed_scope_config) {
            Resolve-ProjectPath ([string]$test.scope_config)
        } elseif ($SessionScopeConfig) { Resolve-ProjectPath $SessionScopeConfig } `
            else { Resolve-ProjectPath ([string]$test.scope_config) }
        $caseCount = @(Import-Csv -LiteralPath $cases | Where-Object {
            $_.enabled -eq 'true' -and [double]$_.VL_V -eq [double]$test.low_voltage_v
        }).Count
        if ($caseCount -ne [int]$test.expected_cases) {
            throw ('Catalog case count mismatch for ' + $test.id + ': expected=' + $test.expected_cases + ', actual=' + $caseCount)
        }
        Write-Output ('=== ' + $test.id + ' | ' + $test.name + ' | ' + $caseCount + ' cases ===')
        $args = @{
            Mode=$Mode; LowVoltage=[int]$test.low_voltage_v; CasesPath=$cases; GroupsPath=$groups
            ScopeConfig=$scopeConfig
        }
        if ($observations) { $args.ObservationsPath = $observations }
        if ($PythonExecutable) { $args.PythonExecutable = $PythonExecutable }
        if ($StopFile) { $args.StopFile = $StopFile }
        if ($Mode -eq 'Run') {
            $testDirectory = Join-Path $batchDirectory ([string]$test.id)
            [void](New-Item -ItemType Directory -Path $testDirectory)
            $args.OutputDirectory = $testDirectory
        }
        $runner = if ($hasCustomRunner) { Resolve-ProjectPath ([string]$test.runner) } `
            else { Join-Path $PSScriptRoot 'run_parameter_sweep.ps1' }
        & $runner @args
        $status.Tests += [ordered]@{Id=[string]$test.id; FinishedAt=(Get-Date).ToString('o'); Status='complete'}
        if ($StopFile -and (Test-Path -LiteralPath ($StopFile + '.used'))) {
            $status.Tests[-1].Status = 'stopped'
            $status.Stopped = $true
            break
        }
    }
    $status.Complete = -not $status.Stopped
} catch {
    $status.Error = $_.Exception.Message
    throw
} finally {
    if ($batchDirectory) {
        # The rig is already disabled and every scope export has completed here.
        # Plotting is a separate result: a rendering failure must not erase a
        # successfully acquired batch or claim the hardware run failed.
        if ($status.Complete -and $customTests.Count -eq 0) {
            $plotScript = Join-Path $ProjectRoot 'scope_bench\plot_hvdc_detail.py'
            $plotPython = if ($PythonExecutable) { $PythonExecutable } `
                else { Join-Path $ProjectRoot 'scope_bench\.venv\Scripts\python.exe' }
            try {
                if (-not (Test-Path -LiteralPath $plotPython)) { throw ('Python missing: ' + $plotPython) }
                if (-not (Test-Path -LiteralPath $plotScript)) { throw ('Plot script missing: ' + $plotScript) }
                & $plotPython $plotScript $batchDirectory
                if ($LASTEXITCODE -ne 0) { throw ('Plot script returned ' + $LASTEXITCODE) }
                $detailIndex = Join-Path $batchDirectory 'hvdc_detail\index.json'
                $status.HVDCDetail = if (Test-Path -LiteralPath $detailIndex) { 'complete' } `
                    else { 'not_applicable' }
            } catch {
                $status.HVDCDetail = 'failed: ' + $_.Exception.Message
                Write-Warning ('HVDC detail plot: ' + $_.Exception.Message)
            }
        }
        if ($status.Complete -and $customTests.Count -gt 0) {
            $status.HVDCDetail = 'dedicated_xi_analysis_in_test_result'
        }
        $status.FinishedAt = (Get-Date).ToString('o')
        $status | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $batchDirectory 'batch_status.json') -Encoding UTF8
    }
}
if ($batchDirectory) { Write-Output ('Selected batch complete: ' + $batchDirectory) }
