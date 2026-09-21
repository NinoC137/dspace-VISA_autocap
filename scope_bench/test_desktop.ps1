param([switch]$Child, [string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
if ($Child) {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    $result = [ordered]@{UI=$false; ReadOnlyCheck=$false; Error=''}
    try {
        & $python (Join-Path $PSScriptRoot 'test_ui_smoke.py') (Join-Path $OutputDirectory 'panel.png') *> (Join-Path $OutputDirectory 'ui.log')
        if ($LASTEXITCODE -ne 0) { throw 'UI smoke test failed.' }
        $result.UI = $true
        & (Join-Path (Split-Path $PSScriptRoot) 'core\run_parameter_sweep.ps1') -Mode Check -LowVoltage 120 `
            -ScopeConfig (Join-Path $PSScriptRoot 'bench_config.json') -PythonExecutable $python *> (Join-Path $OutputDirectory 'check.log')
        $result.ReadOnlyCheck = $true
    } catch { $result.Error = $_.Exception.Message }
    $result | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory 'result.json') -Encoding UTF8
    exit
}
$taskName = 'ScopeBenchReadOnlyTest_' + [guid]::NewGuid().ToString('N')
$OutputDirectory = Join-Path $PSScriptRoot ('desktop_test_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
[void](New-Item -ItemType Directory -Path $OutputDirectory)
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Child -OutputDirectory "' + $OutputDirectory + '"')
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $taskName
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $resultPath = Join-Path $OutputDirectory 'result.json'
    while (-not (Test-Path -LiteralPath $resultPath)) {
        if ($watch.Elapsed.TotalSeconds -gt 60) { throw ('Desktop test timeout: ' + $OutputDirectory) }
        Start-Sleep -Milliseconds 200
    }
    Get-Content -LiteralPath $resultPath
    Write-Output $OutputDirectory
} finally {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
