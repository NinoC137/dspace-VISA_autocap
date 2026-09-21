# Pure planning and measurement sequencing; hardware access is supplied by Rig.
Set-StrictMode -Version 2.0

# The bridge latches a user stop in a marker beside the stop file, which outlives
# the stop file itself. Fakes without a bridge report no stop. Property probing is
# used because StrictMode rejects a plain property read on such objects.
function Test-ScopeStopRequested($Scope) {
    if ($null -eq $Scope) { return $false }
    $match = $Scope.PSObject.Properties.Match('Stopped')
    if ($match.Count -eq 0) { return $false }
    return [bool]$match[0].Value
}

function Save-SweepRecording($Recorder, [string]$ExpectedFile, [double]$TimeoutSeconds = 15) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    do {
        $saved = @($Recorder.LastRecordedFiles)
        if ([int]$Recorder.State -eq 0 -and $saved.Count -eq 1 -and
            [IO.Path]::IsPathRooted([string]$saved[0]) -and
            (Test-Path -LiteralPath $saved[0] -PathType Leaf)) {
            $source = [IO.Path]::GetFullPath([string]$saved[0])
            $target = [IO.Path]::GetFullPath($ExpectedFile)
            # LastRecordedFiles is authoritative: BaseFileName does not select the directory.
            $stream = [IO.File]::OpenRead($source)
            try {
                $header = New-Object byte[] 8
                if ($stream.Length -le 64 -or $stream.Read($header, 0, 8) -ne 8 -or
                    [Text.Encoding]::ASCII.GetString($header) -ne 'MDF     ') {
                    throw ('Invalid or empty MDF file: ' + $source)
                }
            } finally { $stream.Dispose() }
            if ($source -ine $target) { [IO.File]::Copy($source, $target, $false) }
            return
        }
        if ($watch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            throw ('Waveform save timed out: ' + $ExpectedFile + '; RecorderState=' + $Recorder.State +
                '; LastRecordedFiles=' + ($saved -join '; '))
        }
        Start-Sleep -Milliseconds 100
    } while ($true)
}

function Get-SweepCalibrationAccess($Platform, $Description) {
    if ([int]$Platform.CalibrationState -ne 1) {
        throw 'Go Online in ControlDesk first; online calibration is not started.'
    }
    $sets = $Description.DataSets
    $working = $sets.WorkingDataSet
    if ($null -ne $working) {
        return [pscustomobject]@{Mode='WorkingDataSet'; WorkingDataSet=$working}
    }
    # DS1202 with UploadConnectedVariables can be online without any data sets.
    # Enum values verified against the installed ControlDesk 2023-A interop assembly.
    if ([int]$Platform.Type -eq 30 -and
        [int]$Platform.GeneralSettings.StartOnlineCalibrationBehavior -eq 5 -and
        [int]$sets.Count -eq 0 -and $null -eq $sets.ActiveDataSet -and $null -eq $sets.ReferenceDataSet) {
        return [pscustomobject]@{Mode='DirectOnline'; WorkingDataSet=$null}
    }
    throw ('Online calibration is started, but no WorkingDataSet is available in this context. ' +
        'Direct access is supported only for DS1202 / UploadConnectedVariables with no data sets. ' +
        'PlatformType=' + $Platform.Type + '; DataSetCount=' + $sets.Count)
}

function Convert-SweepNumber([string]$Text, [string]$Field) {
    $value = 0.0
    if (-not [double]::TryParse($Text, [Globalization.NumberStyles]::Float,
            [Globalization.CultureInfo]::InvariantCulture, [ref]$value) -or
        [double]::IsNaN($value) -or [double]::IsInfinity($value)) {
        throw "Invalid finite number for ${Field}: '$Text'"
    }
    return $value
}

function Import-SweepCases([string]$Path, [double]$LowVoltage = 0) {
    $ids = @{}
    foreach ($row in (Import-Csv -LiteralPath $Path)) {
        if ($row.enabled -notin @('true', 'false')) { throw 'enabled must be true or false.' }
        if ($row.enabled -eq 'false') { continue }
        if ($row.case_id -notmatch '^[A-Za-z0-9_-]+$' -or $ids.ContainsKey($row.case_id)) {
            throw "Invalid or duplicate case_id: $($row.case_id)"
        }
        $ids[$row.case_id] = $true
        $case = [pscustomobject]@{
            Id=$row.case_id; VL=(Convert-SweepNumber $row.VL_V 'VL_V')
            Xi=(Convert-SweepNumber $row.xi 'xi')
            Ud6=(Convert-SweepNumber $row.ud_ref6 'ud_ref6')
            Ud7=(Convert-SweepNumber $row.ud_ref7 'ud_ref7')
            Phi=(Convert-SweepNumber $row.phi_deg 'phi_deg'); Note=$row.note
        }
        if ($case.VL -le 0 -or $case.Xi -lt 0 -or $case.Xi -gt 1 -or
            $case.Ud6 -lt 0 -or $case.Ud7 -lt 0 -or $case.Phi -lt 0 -or $case.Phi -gt 360) {
            throw "Out-of-domain case: $($case.Id)"
        }
        if ($LowVoltage -eq 0 -or $case.VL -eq $LowVoltage) { $case }
    }
}

function Get-SweepStem([double]$Xi, [double]$Ud6, [double]$Ud7) {
    $culture = [Globalization.CultureInfo]::InvariantCulture
    return 'xi_' + $Xi.ToString('0.########', $culture) + '+' +
        ($Ud6 * [math]::Sqrt(2)).ToString('0.0', $culture) + '_' +
        ($Ud7 * [math]::Sqrt(2)).ToString('0.0', $culture)
}

function Invoke-SweepCase($Rig, $Case, [string]$Directory,
        [double]$Duration = 3, [double]$OffSeconds = 1, $Scope = $null, [string]$StopFile = '') {
    $log = [ordered]@{
        Case=$Case; StartedAt=(Get-Date).ToString('o'); Status='running'
        EnableOnRequestedAt=$null; EnableOffRequestedAt=$null
        EnabledSeconds=$null; Readback=$null; Observations=$null; Files=@(); ScopeManifest=$null; Error=''; CleanupErrors=@()
    }
    $recordingAttempted = $false
    $offWatch = $null
    $failure = $null
    # Assigned before the first instrument operation: the catch block must be able
    # to classify a failure that happens before the enable window is reached.
    $stopped = $false
    try {
        # Honour a stop that arrived before this case touched the rig. Nothing was
        # measured, so nothing is lost: report it and let the caller end the batch.
        if ($StopFile -and (Test-Path -LiteralPath $StopFile)) {
            $stopped = $true
            $log.Status = 'stopped'
            return [pscustomobject]$log
        }
        $Rig.AssertReady()
        $Rig.Write('Enable', 0)
        $Rig.Write('Xi', $Case.Xi)
        $Rig.Write('Ud6', $Case.Ud6)
        $Rig.Write('Ud7', $Case.Ud7)
        $Rig.Write('Phi', $Case.Phi)
        $actual = [pscustomobject]@{
            Xi=$Rig.Read('Xi'); Ud6=$Rig.Read('Ud6'); Ud7=$Rig.Read('Ud7'); Phi=$Rig.Read('Phi')
        }
        $log.Readback = $actual
        $stem = Get-SweepStem $actual.Xi $actual.Ud6 $actual.Ud7
        if ($null -ne $Scope) { $Scope.Prepare($Directory, $log) }
        # Start every recorder before enabling so all files cover the enabled interval.
        $recordingAttempted = $true
        $Rig.StartRecorders($Directory, $stem)
        $log.EnableOnRequestedAt = (Get-Date).ToString('o')
        if ($StopFile -and (Test-Path -LiteralPath $StopFile)) { $stopped = $true; throw 'Stopped by user.' }
        $onWatch = [Diagnostics.Stopwatch]::StartNew()
        $Rig.Write('Enable', 1)
        while ($onWatch.Elapsed.TotalSeconds -lt $Duration) {
            $Rig.CheckRecording()
            if ($null -ne $Scope) { $Scope.Tick($onWatch.Elapsed.TotalSeconds) }
            if (($StopFile -and (Test-Path -LiteralPath $StopFile)) -or (Test-ScopeStopRequested $Scope)) {
                $stopped = $true
                break
            }
            Start-Sleep -Milliseconds 10
        }
        # The frozen waveform is already in scope memory; stopping here would discard
        # it and leave the rig enabled, so the case always runs to a safe disable.
        if ($null -ne $Scope) { $Scope.AssertFrozen() }
        if ($Rig.PSObject.Methods.Match('ReadObservations').Count -gt 0) {
            $log.Observations = $Rig.ReadObservations()
        }
        $log.EnableOffRequestedAt = (Get-Date).ToString('o')
        $Rig.Write('Enable', 0)
        $log.EnabledSeconds = $onWatch.Elapsed.TotalSeconds
        $offWatch = [Diagnostics.Stopwatch]::StartNew()
        $Rig.StopRecorders()
        $recordingAttempted = $false
        $log.Files = @($Rig.VerifyFiles())
        # Export is uninterruptible: it either returns a manifest or aborts the case.
        if ($null -ne $Scope) { $log.ScopeManifest = $Scope.Export() }
        # A stop is an outcome, not a fault: the case above is complete and its
        # artifacts are kept. Report it so the caller can end the batch cleanly.
        if ($stopped -or (Test-ScopeStopRequested $Scope)) {
            $log.Status = 'stopped'
            return [pscustomobject]$log
        }
        $log.Status = 'complete'
    } catch {
        # A user stop is reported by the return above; a throw here is a fault
        # unless the stop was latched while the rig was enabled.
        if (-not $stopped -and (Test-ScopeStopRequested $Scope)) { $stopped = $true }
        $failure = $_
        if ($stopped) { $log.Status = 'stopped' } else { $log.Status = 'failed' }
        $log.Error = $_.Exception.Message
    } finally {
        # Attempt disable first, even if a parameter write or recorder start failed.
        try {
            $Rig.Write('Enable', 0)
            if ($null -eq $offWatch) { $offWatch = [Diagnostics.Stopwatch]::StartNew() }
        } catch {
            $log.CleanupErrors += 'ENABLE DISABLE FAILED: ' + $_.Exception.Message
        }
        if ($recordingAttempted) {
            try { $Rig.StopRecorders() } catch { $log.CleanupErrors += $_.Exception.Message }
        }
        if ($null -ne $Scope) {
            try { $Scope.Cleanup() } catch { $log.CleanupErrors += $_.Exception.Message }
        }
        if ($null -ne $offWatch) {
            $remaining = $OffSeconds - $offWatch.Elapsed.TotalSeconds
            if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int][math]::Ceiling(1000 * $remaining)) }
        }
        if ($log.CleanupErrors.Count -gt 0) { $log.Status = 'failed' }
        $log.FinishedAt = (Get-Date).ToString('o')
        $log | ConvertTo-Json -Depth 12 | Set-Content (Join-Path $Directory 'case.json') -Encoding UTF8
    }
    if ($null -ne $failure) { throw $failure }
    if ($log.CleanupErrors.Count -gt 0) { throw ($log.CleanupErrors -join '; ') }
    return [pscustomobject]$log
}
