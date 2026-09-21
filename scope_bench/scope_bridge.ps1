Set-StrictMode -Version 2.0

function New-ScopeBridge([string]$Python, [string]$Script, [string]$ConfigPath, $Config, [string]$StopFile,
        [string]$MarkerPath = '') {
    $bridge = [pscustomobject]@{
        Python=$Python; Script=$Script; ConfigPath=$ConfigPath; Config=$Config
        StopFile=$StopFile; MarkerPath=$MarkerPath; Process=$null; Directory=''; Begun=$false
        LastState=''; Stopped=$false; AllowStop=$true
    }
    # A user stop is honoured only between instrument operations. Once the frozen
    # waveform is being exported the worker owns the only copy of a 0.5 s record,
    # so an abort there destroys the measurement instead of shortening the run.
    $bridge | Add-Member ScriptMethod CheckStop {
        if (-not $this.AllowStop -or $this.Stopped) { return }
        if (-not ($this.StopFile -and (Test-Path -LiteralPath $this.StopFile))) { return }
        $this.Stopped = $true
        $this.AllowStop = $false
        if ($this.MarkerPath -and -not (Test-Path -LiteralPath $this.MarkerPath)) {
            [IO.File]::WriteAllText($this.MarkerPath, '{}')
        }
    }
    $bridge | Add-Member ScriptMethod Send {
        param([string]$Command)
        $path = Join-Path $this.Directory ($Command + '.json')
        if (Test-Path -LiteralPath $path) { throw ('Duplicate scope command: ' + $Command) }
        [IO.File]::WriteAllText($path, '{}')
    }
    $bridge | Add-Member ScriptMethod Poll {
        $path = Join-Path $this.Directory 'status.json'
        if (Test-Path -LiteralPath $path) {
            $status = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            if ($status.state -ne $this.LastState) {
                Write-Host ('Scope: ' + $status.state)
                $this.LastState = $status.state
                if ($status.state -eq 'complete' -and $status.PSObject.Properties['warnings']) {
                    foreach ($message in $status.warnings) { Write-Warning $message }
                }
            }
            if ($status.state -eq 'failed') { throw ('Scope: ' + $status.error) }
        } else { $status = [pscustomobject]@{state='starting'} }
        if ($status.state -eq 'complete') { return $status.state }
        $this.CheckStop()
        if ($this.Process.HasExited) {
            throw ('Scope worker exited. See ' + (Join-Path $this.Directory 'stderr.log'))
        }
        return $status.state
    }
    $bridge | Add-Member ScriptMethod Sleep {
        param([int]$Milliseconds)
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            if ($this.Poll() -eq 'complete') { return }
            if ($watch.Elapsed.TotalMilliseconds -ge $Milliseconds) { return }
            Start-Sleep -Milliseconds 25
        }
    }
    $bridge | Add-Member ScriptMethod Prepare {
        param([string]$CaseDirectory, $Context)
        $this.CheckStop()
        $this.Directory = Join-Path $CaseDirectory ('scope\capture_' + [guid]::NewGuid().ToString('N'))
        [void](New-Item -ItemType Directory -Path $this.Directory)
        $Context | ConvertTo-Json -Depth 12 | Set-Content (Join-Path $this.Directory 'context.json') -Encoding UTF8
        # Windows CreateProcess needs quoted file arguments; embedded quotes are rejected upstream.
        $argLine = '"' + $this.Script + '" worker --config "' + $this.ConfigPath + '" --directory "' + $this.Directory + '"'
        $this.Process = Start-Process -FilePath $this.Python -ArgumentList $argLine -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $this.Directory 'stdout.log') `
            -RedirectStandardError (Join-Path $this.Directory 'stderr.log')
        # Cache the handle while the child is alive; Windows PowerShell otherwise may lose ExitCode.
        [void]$this.Process.Handle
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($this.Poll() -ne 'prepared') {
            if ($watch.Elapsed.TotalSeconds -gt 120) { throw 'Scope prepare timed out.' }
            Start-Sleep -Milliseconds 50
        }
        $this.Begun = $false
    }
    $bridge | Add-Member ScriptMethod Tick {
        param([double]$Elapsed)
        $state = $this.Poll()
        if (-not $this.Begun -and $Elapsed -ge $this.Config.timing.settle_s) {
            $this.Send('acquire')
            $this.Begun = $true
        }
        $deadline = $this.Config.timing.settle_s + $this.Config.instrument.acquisition_timeout_s
        if ($Elapsed -gt $deadline -and $state -ne 'frozen') { throw 'Scope acquisition deadline missed.' }
    }
    $bridge | Add-Member ScriptMethod AssertFrozen {
        if (-not $this.Begun -or $this.Poll() -ne 'frozen') { throw 'No frozen waveform before disable.' }
    }
    $bridge | Add-Member ScriptMethod Export {
        if ($this.Stopped) { throw 'Stopped by user.' }
        # From here to completion the payload is uninterruptible: a stop requested
        # during the export is honoured after it finishes, never by discarding it.
        $this.AllowStop = $false
        try {
            $this.Send('export')
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $settings = Get-Content (Join-Path $this.Directory 'settings.json') -Raw | ConvertFrom-Json
            $bytesPerChannel = 4.0 * [double]$settings.acquisition.record_length
            $channelCount = @($this.Config.channels.PSObject.Properties | Where-Object { $_.Value.enabled }).Count
            $transferSeconds = [Math]::Max($this.Config.instrument.export_timeout_s,
                2 * $bytesPerChannel / $this.Config.instrument.min_throughput_bytes_per_s)
            $deadline = 180 + $channelCount * $this.Config.instrument.query_attempts * ($transferSeconds + 30)
            # Old configuration files receive the same defaults in Python.
            $shotCount = 5
            $shotSeconds = 30
            if ($this.Config.PSObject.Properties['screenshots']) {
                $shots = $this.Config.screenshots
                if ($shots.PSObject.Properties['enabled'] -and -not $shots.enabled) { $shotCount = 0 }
                elseif ($shots.PSObject.Properties['spans_s']) { $shotCount = @($shots.spans_s).Count }
                if ($shots.PSObject.Properties['timeout_s']) { $shotSeconds = $shots.timeout_s }
            }
            $deadline += 120 + $shotCount * ($this.Config.instrument.query_attempts * $shotSeconds + 30)
            while ($this.Poll() -ne 'complete') {
                if ($watch.Elapsed.TotalSeconds -gt $deadline) { throw 'Scope export/analysis timed out.' }
                Start-Sleep -Milliseconds 100
            }
            if (-not $this.Process.WaitForExit(10000)) { throw 'Scope worker cleanup timed out.' }
            if ($this.Process.ExitCode -ne 0) { throw 'Scope worker cleanup failed.' }
            # A stop that arrived while the payload was being written is latched
            # here, after the payload is safe, and ends the batch at the caller.
            $this.AllowStop = $true
            $this.CheckStop()
            return (Join-Path $this.Directory 'manifest.json')
        } finally { $this.AllowStop = $true }
    }
    $bridge | Add-Member ScriptMethod Cleanup {
        if ($null -eq $this.Process) { return }
        try {
            if ($this.Process.HasExited) { return }
            # The worker only reads abort between operations, so this is best effort:
            # if it is inside a transfer the StopFile alone could never reach it.
            [IO.File]::WriteAllText((Join-Path $this.Directory 'abort.json'), '{}')
            if (-not $this.Process.WaitForExit(5000)) {
                $this.Process.Kill()
                $this.Process.WaitForExit()
                throw ('Worker terminated after abort timeout; partial data retained in ' + $this.Directory)
            }
        } finally {
            $this.Process.Dispose()
            $this.Process=$null
            # A stop latched in this case stops this run only; never the next batch.
            if ($this.StopFile -and (Test-Path -LiteralPath $this.StopFile)) {
                Remove-Item -LiteralPath $this.StopFile -Force
            }
        }
    }
    return $bridge
}
