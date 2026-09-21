$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }

$tokens = $null
$parseErrors = $null
[void][Management.Automation.Language.Parser]::ParseFile(
    (Join-Path $ProjectRoot 'core\run_test_batch.ps1'), [ref]$tokens, [ref]$parseErrors)
Assert ($parseErrors.Count -eq 0) 'Batch runner must parse under Windows PowerShell.'

$catalog = Get-Content (Join-Path $ProjectRoot 'test_cases\catalog.json') -Raw | ConvertFrom-Json
Assert ($catalog.schema_version -eq 1) 'Unexpected catalog schema.'
$tests = @($catalog.tests | ForEach-Object { $_ })
Assert ($tests.Count -ge 5) 'Expected the phase test and four legacy VL groups.'
$ids = @{}
foreach ($test in $tests) {
    Assert (-not $ids.ContainsKey([string]$test.id)) ('Duplicate test id: ' + $test.id)
    $ids[[string]$test.id] = $true
    foreach ($field in @('cases','groups','scope_config')) {
        Assert (Test-Path (Join-Path $ProjectRoot ([string]$test.$field))) ("Missing $field for $($test.id)")
    }
    $cases = @(Import-Csv (Join-Path $ProjectRoot ([string]$test.cases)) | Where-Object {
        $_.enabled -eq 'true' -and [double]$_.VL_V -eq [double]$test.low_voltage_v
    })
    Assert ($cases.Count -eq [int]$test.expected_cases) ('Case count mismatch: ' + $test.id)
}
Assert ($ids.ContainsKey('phase_angle_validation_01')) 'Phase validation test is not registered.'

$runner = Join-Path $ProjectRoot 'core\run_test_batch.ps1'
foreach ($bad in @('', 'unknown_test', 'phase_angle_validation_01,phase_angle_validation_01')) {
    $failed = $false
    try { & $runner -Mode Plan -TestIds $bad | Out-Null } catch { $failed = $true }
    Assert $failed ('Invalid selection accepted: ' + $bad)
}
$failed = $false
try { & $runner -Mode Run -TestIds 'phase_angle_validation_01,legacy_vl140' | Out-Null }
catch { $failed = $_.Exception.Message -like '*manual supply setups*' }
Assert $failed 'A batch crossing manual supply settings must be rejected before execution.'

Write-Output 'PASS: catalog paths/counts, selection validation and manual-supply batch guard.'
