[CmdletBinding()]
param(
    [ValidateSet('Plan','Check','Run')][string]$Mode = 'Plan',
    [ValidateSet(0,100,120,140,160)][int]$LowVoltage = 0,
    [string]$CasesPath = '', [string]$GroupsPath = '', [string]$ObservationsPath = '', [string]$OutputDirectory = '',
    [string]$ScopeConfig = '', [string]$PythonExecutable = '', [string]$StopFile = '',
    [double]$Duration = 0
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
Set-StrictMode -Version 2.0
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'sweep_core.ps1')
if (-not $CasesPath) { $CasesPath = Join-Path $ProjectRoot 'config\sweep_cases.csv' }
if (-not $GroupsPath) { $GroupsPath = Join-Path $ProjectRoot 'config\waveform_groups.json' }
$scope = $null
$offSeconds = 1.0
# The stop marker outlives the stop file that Cleanup removes, so this run can
# still report a user stop after the case that honoured it has finished.
$stopMarker = ''
if ($StopFile) { $stopMarker = $StopFile + '.used' }
if ($ScopeConfig) {
    $ScopeConfig = (Resolve-Path -LiteralPath $ScopeConfig).Path
    $scopeScript = Join-Path $ProjectRoot 'scope_bench\bench.py'
    if (-not $PythonExecutable) { $PythonExecutable = Join-Path $ProjectRoot 'scope_bench\.venv\Scripts\python.exe' }
    foreach ($p in @($PythonExecutable, $scopeScript, $ScopeConfig)) {
        if ($p.Contains('"') -or -not (Test-Path -LiteralPath $p)) { throw ('Invalid/missing path: ' + $p) }
    }
    & $PythonExecutable $scopeScript plan --config $ScopeConfig
    if ($LASTEXITCODE -ne 0) { throw 'Invalid scope configuration.' }
    $scopeSettings = Get-Content -LiteralPath $ScopeConfig -Raw | ConvertFrom-Json
    if ($Duration -ne 0 -and $Duration -ne $scopeSettings.timing.enable_duration_s) {
        throw 'Set enable duration in ScopeConfig; conflicting Duration was supplied.'
    }
    $Duration = $scopeSettings.timing.enable_duration_s
    $offSeconds = $scopeSettings.timing.minimum_off_s
    . (Join-Path $ProjectRoot 'scope_bench\scope_bridge.ps1')
    $scope = New-ScopeBridge $PythonExecutable $scopeScript $ScopeConfig $scopeSettings $StopFile $stopMarker
} elseif ($Duration -eq 0) { $Duration = 3 }
if (-not $OutputDirectory) {
    if ($ScopeConfig -and $scopeSettings.instrument.PSObject.Properties['transport'] -and
            $scopeSettings.instrument.transport -eq 'raspi_ssh') {
        $OutputDirectory = Join-Path $scopeSettings.instrument.remote.local_share_root 'sweeps'
    } else { $OutputDirectory = Join-Path $ProjectRoot 'sweep_results' }
}
if ([double]::IsNaN($Duration) -or [double]::IsInfinity($Duration) -or $Duration -le 0 -or $Duration -gt 15) {
    throw 'Duration must be finite and between 0 and 15 seconds.'
}
$cases = @(Import-SweepCases $CasesPath $LowVoltage)
if (-not $cases.Count) { throw 'No enabled cases selected.' }
$groups = Get-Content -LiteralPath $GroupsPath -Raw | ConvertFrom-Json
$observationSpecs = @()
if ($ObservationsPath) {
    $ObservationsPath = (Resolve-Path -LiteralPath $ObservationsPath).Path
    $parsedObservationSpecs = Get-Content -LiteralPath $ObservationsPath -Raw | ConvertFrom-Json
    $observationSpecs = @($parsedObservationSpecs | ForEach-Object { $_ })
    $observationNames = @{}
    foreach ($spec in $observationSpecs) {
        if ($spec.Name -notmatch '^[A-Za-z][A-Za-z0-9_]*$' -or $observationNames.ContainsKey($spec.Name) -or
                -not $spec.Path) { throw 'Observation names must be unique and paths must be nonempty.' }
        $observationNames[$spec.Name] = $true
    }
}
$names = @{}
foreach ($g in $groups) {
    if ($g.Name -notmatch '^[A-Za-z0-9_]+$' -or $names.ContainsKey($g.Name) -or @($g.Signals).Count -eq 0) {
        throw 'Waveform groups need unique filename-safe names and nonempty signal lists.'
    }
    $names[$g.Name] = $true
}
if (@($groups).Count -eq 0) { throw 'No waveform groups.' }
$plan = @($cases | ForEach-Object {
    [pscustomobject]@{Case=$_.Id; VL=$_.VL; Xi=$_.Xi; Ud6=$_.Ud6; Ud7=$_.Ud7; Phi=$_.Phi
        EnableSeconds=$Duration; MinimumOffSeconds=$offSeconds; FilePrefix=(Get-SweepStem $_.Xi $_.Ud6 $_.Ud7)}
})
$plan | Format-Table -AutoSize | Out-Host
if ($Mode -eq 'Plan') { return }
if ($Mode -eq 'Run' -and $LowVoltage -eq 0) { throw 'Select exactly one manually adjusted VL using -LowVoltage.' }

function Read-ComScalar($Object, [string]$Property = 'ValueConverted') {
    $value = $Object.GetType().InvokeMember($Property, [Reflection.BindingFlags]::GetProperty, $null, $Object, $null)
    if ($null -eq $value -or $value -is [DBNull] -or $value -is [Array]) { throw "Unavailable/non-scalar $Property." }
    $number = [double]$value
    if ([double]::IsNaN($number) -or [double]::IsInfinity($number)) { throw "Non-finite $Property." }
    return $number
}

$app = [Runtime.InteropServices.Marshal]::GetActiveObject('ControlDeskNG.Application')
if ($app.ActiveProject.Name -ne 'Project_PACC' -or $app.ActiveExperiment.Name -ne 'Experiment_PACC') {
    throw 'Expected the already open Project_PACC / Experiment_PACC.'
}
$platform = $app.ActiveExperiment.Platforms.Item('Platform')
$description = $platform.ActiveVariableDescription
$paths = @{
    Enable='Model Root/Enable1/Value'; Xi='Model Root/Constant3/Value'
    Ud6='Model Root/ud_ref6/Value'; Ud7='Model Root/ud_ref7/Value'; Phi='Model Root/phi2_deg/Value'
}
$variables = @{}
foreach ($key in $paths.Keys) {
    try {
        $v = $description.Variables.Item($paths[$key])
    } catch {
        throw [InvalidOperationException]::new(
            ('Cannot resolve sweep parameter ' + $key + ': ' + $paths[$key] + '. ' + $_.Exception.Message),
            $_.Exception)
    }
    if ($null -eq $v) { throw ('Sweep parameter not found: ' + $paths[$key]) }
    if (-not $v.IsWritable -or -not $v.IsReadable) { throw "Parameter not readable/writable: $($paths[$key])" }
    $variables[$key] = $v
}
$observationVariables = @{}
foreach ($spec in $observationSpecs) {
    try { $v = $description.Variables.Item([string]$spec.Path) }
    catch { throw ('Cannot resolve observation ' + $spec.Name + ': ' + $spec.Path) }
    if ($null -eq $v -or -not $v.IsReadable) { throw ('Observation is not readable: ' + $spec.Name) }
    $observationVariables[[string]$spec.Name] = $v
}
$mdm = $app.MeasurementDataManagement
# Reuse the exact live measurement definitions; do not replace the user's signal list.
foreach ($g in $groups) {
    $layouts = @($app.LayoutManagement.Layouts | Where-Object Name -eq $g.Layout)
    if ($layouts.Count -ne 1) { throw ('Expected one loaded layout: ' + $g.Layout) }
    $instruments = @($layouts[0].Instruments | Where-Object Name -eq $g.Instrument)
    if ($instruments.Count -ne 1) { throw ('Expected one instrument: ' + $g.Instrument) }
    $instrument = $instruments[0]
    $displayed = @($instrument.Plots | ForEach-Object {
        foreach ($axis in $_.YAxes) { foreach ($s in $axis.Signals) { [string]$s.MainVariable.Path } }
    })
    foreach ($signal in $g.Signals) {
        if ($signal -cnotin $displayed) { throw "Signal missing from $($g.Instrument): $signal" }
        if (-not $mdm.MeasurementConfiguration.Signals.Contains($signal)) { throw "Signal not in measurement list: $signal" }
        if (-not $mdm.MeasurementConfiguration.Signals.Item($signal).Active) { throw "Measurement signal inactive: $signal" }
    }
}
if ($Mode -eq 'Check') {
    if ([int]$platform.CalibrationState -eq 1) {
        $access = Get-SweepCalibrationAccess $platform $description
        foreach ($key in $variables.Keys) { [void](Read-ComScalar $variables[$key]) }
        if ((Read-ComScalar $variables.Enable) -ne 0) { throw 'Enable1 must be 0 before starting a batch.' }
        Write-Output ('ONLINE PARAMETER CHECK OK: access=' + $access.Mode + '; Enable1=0')
    } else {
        Write-Output 'OFFLINE: configuration check only; Go Online before starting a batch.'
    }
    if ($null -ne $scope) {
        & $PythonExecutable $scopeScript check --config $ScopeConfig
        if ($LASTEXITCODE -ne 0) { throw 'Scope read-only check failed.' }
    }
    Write-Output ('CHECK OK: parameter paths and ' + @($groups).Count + ' waveform groups; CalibrationState=' + $platform.CalibrationState)
    Write-Output 'Read-only check. No parameters, recorders or measurement state changed.'
    return
}

$mutex = [Threading.Mutex]::new($false, 'Local\ControlDesk_PACC_ParameterSweep')
$locked = $false
$recorders = [Collections.Generic.List[object]]::new()
$startedMeasurement = $false
$armed = $false
$runDirectory = $null
$rig = $null
$runError = $null
$cleanupErrors = [Collections.Generic.List[string]]::new()
try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another sweep is running in this desktop session.' }
    # The mutex is held, so no sibling run is using these. A request or marker left
    # behind by a crashed run would otherwise abort this batch before its first case.
    if ($StopFile) {
        foreach ($stale in @($StopFile, $StopFile + '.used')) {
            if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Force }
        }
    }
    if ([int]$platform.CalibrationState -ne 1) { throw 'Go Online in ControlDesk first; this script does not download/start the real-time application.' }
    if ((Read-ComScalar $variables.Enable) -ne 0) { throw 'Enable1 must be 0 before starting a batch.' }
    if ([int]$mdm.MainRecorder.State -ne 0) { throw 'Main recorder is already recording.' }
    foreach ($r in $mdm.Recorders) { if ([int]$r.State -ne 0) { throw ('Recorder already running: ' + $r.Name) } }
    $access = Get-SweepCalibrationAccess $platform $description
    foreach ($key in $variables.Keys) { [void](Read-ComScalar $variables[$key]) }
    Write-Output ('Calibration access: ' + $access.Mode)
    $armed = $true
    if ($null -ne $access.WorkingDataSet) { $access.WorkingDataSet.Activate() }
    if ((Read-ComScalar $variables.Enable) -ne 0) { throw 'Calibration context Enable1 is not 0.' }
    $runDirectory = Join-Path $OutputDirectory ('VL_' + $LowVoltage + '\run_' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff') + '_' + $PID)
    [void](New-Item -ItemType Directory -Path $runDirectory)
    $plan | Export-Csv (Join-Path $runDirectory 'plan.csv') -NoTypeInformation -Encoding UTF8
    Copy-Item -LiteralPath $GroupsPath -Destination (Join-Path $runDirectory 'waveform_groups.json')
    if ($ObservationsPath) { Copy-Item -LiteralPath $ObservationsPath -Destination (Join-Path $runDirectory 'observation_signals.json') }
    if ($ScopeConfig) { Copy-Item -LiteralPath $ScopeConfig -Destination (Join-Path $runDirectory 'bench_config.json') }
    if (-not $mdm.IsMeasuring) {
        $startedMeasurement = $true
        $mdm.Start()
    }
    if (-not $mdm.IsMeasuring) { throw 'Measurement did not start.' }
    foreach ($g in $groups) {
        $r = $mdm.Recorders.Add('AutoSweep_' + $PID + '_' + $g.Name)
        $recorders.Add([pscustomobject]@{Group=$g; Recorder=$r; ExpectedFile=''})
        $r.Active = $true
        $r.AutomaticSignalConfigurationEnabled = $false
        $r.Signals.RemoveAll()
        foreach ($signal in $g.Signals) {
            $s = $mdm.MeasurementConfiguration.Signals.Item($signal)
            $r.Signals.Insert($s)
            $r.Signals.SetRecordedEnabled($s, $true)
        }
        if ([int]$r.Signals.Count -ne @($g.Signals).Count) { throw 'Recorder signal count mismatch.' }
        $r.AutomaticNamingEnabled = $false
        $r.AutomaticSaveDialogEnabled = $false
        $r.AutomaticExportRecordingEnabled = $false
        $r.AddToExperimentEnabled = $false
        $r.OpenInMeasurementDataPoolEnabled = $false
        $r.MeasurementTimestampsEnabled = $true
        $r.WriteToFileEnabled = $true
        $r.StopCondition.Enabled = $false
    }
    $rig = [pscustomobject]@{App=$app; Platform=$platform; Mdm=$mdm; Variables=$variables
        ObservationVariables=$observationVariables; Recorders=$recorders}
    $rig | Add-Member ScriptMethod Read {
        param($Key)
        return Read-ComScalar $this.Variables[$Key]
    }
    $rig | Add-Member ScriptMethod Write {
        param($Key, [double]$Value)
        $v = $this.Variables[$Key]
        $v.ValueConverted = $Value
        $actual = Read-ComScalar $v
        if ([math]::Abs($actual - $Value) -gt 1e-7 * [math]::Max(1, [math]::Abs($Value))) {
            throw "Write/readback mismatch for ${Key}: requested=$Value actual=$actual"
        }
    }
    $rig | Add-Member ScriptMethod ReadObservations {
        $values = [ordered]@{ReadAt=(Get-Date).ToString('o')}
        foreach ($name in $this.ObservationVariables.Keys) {
            $raw = $this.ObservationVariables[$name].GetType().InvokeMember('ValueConverted',
                [Reflection.BindingFlags]::GetProperty, $null, $this.ObservationVariables[$name], $null)
            if ($null -eq $raw -or $raw -is [DBNull]) { throw ('Observation unavailable: ' + $name) }
            if ($raw -is [Array]) { $values[$name] = @($raw | ForEach-Object { [double]$_ }) }
            else { $values[$name] = [double]$raw }
        }
        return [pscustomobject]$values
    }
    $rig | Add-Member ScriptMethod AssertReady {
        if ($this.App.ActiveProject.Name -ne 'Project_PACC' -or $this.App.ActiveExperiment.Name -ne 'Experiment_PACC' -or
            [int]$this.Platform.CalibrationState -ne 1 -or -not $this.Mdm.IsMeasuring) { throw 'Experiment/online/measurement state changed.' }
        if ($this.Read('Enable') -ne 0) { throw 'Enable1 was changed outside the sweep.' }
    }
    $rig | Add-Member ScriptMethod StartRecorders {
        param($Directory, $Stem)
        foreach ($entry in $this.Recorders) {
            $entry.ExpectedFile = Join-Path $Directory ($Stem + '+' + $entry.Group.Name + '.mf4')
            if (Test-Path -LiteralPath $entry.ExpectedFile) { throw 'Refusing to overwrite an existing waveform.' }
            # ControlDesk writes to its experiment folder, ignoring directories in BaseFileName.
            # A unique source name prevents cases/runs from overwriting each other there.
            $entry.Recorder.BaseFileName = $Stem + '+' + $entry.Group.Name + '_' + [guid]::NewGuid().ToString('N') + '.mf4'
            $entry.Recorder.Start($false, $false)
        }
        $this.CheckRecording()
    }
    $rig | Add-Member ScriptMethod CheckRecording {
        if ([int]$this.Platform.CalibrationState -ne 1 -or -not $this.Mdm.IsMeasuring) { throw 'Lost online measurement.' }
        foreach ($entry in $this.Recorders) {
            if ([int]$entry.Recorder.State -ne 2) { throw ('Recorder is not running: ' + $entry.Group.Name) }
        }
    }
    $rig | Add-Member ScriptMethod StopRecorders {
        $errors = @()
        foreach ($entry in $this.Recorders) {
            try { if ([int]$entry.Recorder.State -ne 0) { $entry.Recorder.Stop() } }
            catch { $errors += $_.Exception.Message }
        }
        if ($errors.Count) { throw ($errors -join '; ') }
    }
    $rig | Add-Member ScriptMethod VerifyFiles {
        foreach ($entry in $this.Recorders) {
            Save-SweepRecording $entry.Recorder $entry.ExpectedFile
            $stream = [IO.File]::OpenRead($entry.ExpectedFile)
            try {
                $header = New-Object byte[] 8
                if ($stream.Length -le 64 -or $stream.Read($header, 0, 8) -ne 8 -or
                    [Text.Encoding]::ASCII.GetString($header) -ne 'MDF     ') { throw 'Invalid or empty MDF file.' }
            } finally { $stream.Dispose() }
            [pscustomobject]@{Group=$entry.Group.Name; File=$entry.ExpectedFile
                Bytes=(Get-Item -LiteralPath $entry.ExpectedFile).Length
                Sha256=(Get-FileHash -LiteralPath $entry.ExpectedFile -Algorithm SHA256).Hash}
        }
    }
    foreach ($case in $cases) {
        $directory = Join-Path $runDirectory ('case_' + $case.Id + '_phi_' + $case.Phi)
        [void](New-Item -ItemType Directory -Path $directory)
        Write-Output ('Measuring case ' + $case.Id + ', xi=' + $case.Xi + ', phi=' + $case.Phi)
        $result = Invoke-SweepCase $rig $case $directory -Duration $Duration -OffSeconds $offSeconds -Scope $scope -StopFile $StopFile
        if ($null -ne $scope) {
            if (-not (Test-Path -LiteralPath $scope.MarkerPath)) {
                & $PythonExecutable $scopeScript summarize --directory $runDirectory
                if ($LASTEXITCODE -ne 0) { throw 'Scope summary generation failed.' }
            }
        }
        Write-Output ('Saved: ' + $directory + ' (' + $result.EnabledSeconds.ToString('F3') + ' s enable interval)')
        if (Test-Path -LiteralPath $scope.MarkerPath) { break }
    }
} catch {
    $runError = $_
} finally {
    if ($armed) {
        try {
            $variables.Enable.ValueConverted = 0.0
            if ((Read-ComScalar $variables.Enable) -ne 0) { throw 'Enable1 readback is not 0.' }
        } catch { $cleanupErrors.Add('ENABLE DISABLE FAILED: ' + $_.Exception.Message) }
    }
    foreach ($entry in $recorders) {
        try {
            if ([int]$entry.Recorder.State -ne 0) { $entry.Recorder.Stop() }
            $entry.Recorder.Remove()
        } catch { $cleanupErrors.Add('Recorder cleanup: ' + $_.Exception.Message) }
    }
    if ($startedMeasurement) {
        try { $mdm.Stop() } catch { $cleanupErrors.Add('Measurement cleanup: ' + $_.Exception.Message) }
    }
    if ($null -ne $runDirectory) {
        $errorText = if ($null -eq $runError) { '' } else { $runError.Exception.Message }
        $stopped = [bool]($StopFile -and (Test-Path -LiteralPath $stopMarker))
        [ordered]@{FinishedAt=(Get-Date).ToString('o'); VLManualSetting=$LowVoltage
            Error=$errorText; CleanupErrors=@($cleanupErrors.ToArray())
            Stopped=$stopped
            Complete=($null -eq $runError -and $cleanupErrors.Count -eq 0)} |
            ConvertTo-Json -Depth 8 | Set-Content (Join-Path $runDirectory 'run_status.json') -Encoding UTF8
    }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
# A requested stop is a normal outcome: every artifact of the finished case is kept.
if ($StopFile -and (Test-Path -LiteralPath $stopMarker)) {
    Write-Output ('Stopped by user after ' + $runDirectory + '; Enable1=0, partial results kept.')
    return
}
if ($null -ne $runError) { throw $runError }
if ($cleanupErrors.Count) { throw ($cleanupErrors -join '; ') }
Write-Output ('Batch complete, Enable1=0. Output: ' + $runDirectory)
