param([string]$Python = 'python')
# Regression guard for the failure that discarded a finished measurement: the
# operator pressed Stop while the frozen waveform was being exported, the bridge
# threw "Stopped by user." from inside Poll, and Cleanup then killed the worker,
# leaving only a truncated waveforms.h5.partial. Export must be uninterruptible.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scope_bridge.ps1')
$root = Join-Path ([IO.Path]::GetTempPath()) ('Scope Stop Test ' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $root)
$config = [pscustomobject]@{
    timing=[pscustomobject]@{settle_s=0}
    instrument=[pscustomobject]@{acquisition_timeout_s=5; export_timeout_s=1; min_throughput_bytes_per_s=200000; query_attempts=2}
    channels=[pscustomobject]@{CH1=[pscustomobject]@{enabled=$true}}
}
$worker = Join-Path $PSScriptRoot 'test_export_worker.py'
$stopFile = Join-Path $root 'stop.json'
$marker = $stopFile + '.used'
$bridge = New-ScopeBridge $Python $worker 'unused.json' $config $stopFile $marker

function Wait-State([string]$Wanted) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($bridge.Poll() -ne $Wanted) {
        if ($watch.Elapsed.TotalSeconds -gt 30) { throw ('Fixture timeout waiting for ' + $Wanted) }
        Start-Sleep -Milliseconds 20
    }
}

try {
    # A stop that arrives while the rig is enabled must not abort the payload.
    $bridge.Prepare($root, @{Case=@{};Readback=@{}})
    $bridge.Begun = $true
    $bridge.Send('acquire')
    Wait-State 'frozen'
    [IO.File]::WriteAllText($stopFile, '{}')
    $manifest = $null
    $threw = $false
    try { $manifest = $bridge.Export() } catch { $threw = $true; $script:exportError = $_.Exception.Message }
    if ($threw) { throw ('Export aborted by the stop request: ' + $script:exportError) }
    if (-not (Test-Path -LiteralPath $manifest)) { throw 'Manifest was not produced.' }
    if (-not $bridge.Stopped) { throw 'The pending stop must still be latched for the caller.' }
    if (-not (Test-Path -LiteralPath $marker)) { throw 'The stop marker must outlive the stop file.' }
    # Cleanup clears the request so the next batch starts, and keeps the marker so
    # this run can still report why it ended.
    $bridge.Cleanup()
    if (Test-Path -LiteralPath $stopFile) { throw 'Cleanup must consume the stop request.' }
    if (-not (Test-Path -LiteralPath $marker)) { throw 'Cleanup must keep the stop marker.' }

    # A stop that is already pending before the payload exists cannot save anything,
    # so it must abort without exporting rather than overwrite the previous case.
    Remove-Item -LiteralPath $marker -Force
    $bridge = New-ScopeBridge $Python $worker 'unused.json' $config $stopFile $marker
    $bridge.Prepare($root, @{Case=@{};Readback=@{}})
    [IO.File]::WriteAllText($stopFile, '{}')
    $bridge.Begun = $true
    $bridge.Send('acquire')
    Wait-State 'frozen'
    $threw = $false
    try { $bridge.Export() | Out-Null } catch { $threw = $true }
    if (-not $threw) { throw 'A stop pending before export must skip the export.' }
    if (-not (Test-Path -LiteralPath $marker)) { throw 'The skipped export must still record the stop.' }
    Write-Output 'PASS: export survives a stop request, latches it, cleanup consumes the request and keeps the reason.'
} finally {
    try { $bridge.Cleanup() } catch { }
    Remove-Item -LiteralPath $root -Recurse -Force
}
