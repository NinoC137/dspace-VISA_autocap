param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scope_bridge.ps1')
$root = Join-Path ([IO.Path]::GetTempPath()) ('Scope Bridge Test ' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $root)
$config = [pscustomobject]@{
    timing=[pscustomobject]@{settle_s=0}
    instrument=[pscustomobject]@{acquisition_timeout_s=5; export_timeout_s=1; min_throughput_bytes_per_s=200000; query_attempts=2}
    channels=[pscustomobject]@{CH1=[pscustomobject]@{enabled=$true}}
}
$bridge = New-ScopeBridge $Python (Join-Path $PSScriptRoot 'test_worker_process.py') 'unused.json' $config ''
try {
    $bridge.Prepare($root, @{Case=@{};Readback=@{}})
    $bridge.Tick(0.01)
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($bridge.Poll() -ne 'frozen') {
        if ($watch.Elapsed.TotalSeconds -gt 5) { throw 'Fixture freeze timeout.' }
        Start-Sleep -Milliseconds 20
    }
    $bridge.AssertFrozen()
    $manifest = $bridge.Export()
    if (-not (Test-Path -LiteralPath $manifest)) { throw 'Fixture manifest missing.' }
    Write-Output 'PASS: Windows process launch, paths with spaces, atomic IPC, freeze/export handshake and process exit.'
} finally {
    $bridge.Cleanup()
    Remove-Item -LiteralPath $root -Recurse -Force
}
