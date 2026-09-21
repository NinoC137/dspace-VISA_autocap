# Contract tests with an in-memory fake rig. No COM or hardware access.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $ProjectRoot 'core\sweep_core.ps1')
function Assert($Condition, $Message) { if (-not $Condition) { throw $Message } }

# Regression: an online DS1202 using UploadConnectedVariables has no data sets.
$platform = [pscustomobject]@{CalibrationState=1; Type=30
    GeneralSettings=[pscustomobject]@{StartOnlineCalibrationBehavior=5}}
$sets = [pscustomobject]@{Count=0; WorkingDataSet=$null; ActiveDataSet=$null; ReferenceDataSet=$null}
$description = [pscustomobject]@{DataSets=$sets}
Assert ((Get-SweepCalibrationAccess $platform $description).Mode -eq 'DirectOnline') 'Online DS1202 must not require a dataset.'
foreach ($fault in @('offline', 'other_platform', 'other_behavior', 'saved_set', 'active_set', 'reference_set')) {
    $platform.CalibrationState=1; $platform.Type=30
    $platform.GeneralSettings.StartOnlineCalibrationBehavior=5
    $sets.Count=0; $sets.ActiveDataSet=$null; $sets.ReferenceDataSet=$null
    switch ($fault) {
        'offline' { $platform.CalibrationState=0 }
        'other_platform' { $platform.Type=4 }
        'other_behavior' { $platform.GeneralSettings.StartOnlineCalibrationBehavior=2 }
        'saved_set' { $sets.Count=1 }
        'active_set' { $sets.ActiveDataSet=[pscustomobject]@{Name='saved'} }
        'reference_set' { $sets.ReferenceDataSet=[pscustomobject]@{Name='reference'} }
    }
    $failed=$false
    try { Get-SweepCalibrationAccess $platform $description | Out-Null } catch { $failed=$true }
    Assert $failed ('Ambiguous/offline context must be rejected: ' + $fault)
}
$platform.CalibrationState=1
$working = [pscustomobject]@{Activations=0}
$working | Add-Member ScriptMethod Activate { $this.Activations++ }
$sets.WorkingDataSet=$working
$access = Get-SweepCalibrationAccess $platform $description
Assert ($access.Mode -eq 'WorkingDataSet' -and $access.WorkingDataSet -eq $working) 'Existing working dataset must remain supported.'
Assert ($working.Activations -eq 0) 'Read-only access check must not activate datasets.'
Write-Output 'PASS: calibration access (direct online, working dataset, six rejected contexts; no activation).'

function New-FakeRig([string]$Fault = '') {
    $rig = [pscustomobject]@{
        Values=@{Enable=0.0; Xi=0.4; Ud6=60.0; Ud7=60.0; Phi=0.0}
        Events=[Collections.Generic.List[string]]::new(); Fault=$Fault
        Recording=$false; OnAt=0L; OffAt=0L
    }
    $rig | Add-Member ScriptMethod AssertReady { $this.Events.Add('ready') }
    $rig | Add-Member ScriptMethod Write {
        param($Key, $Value)
        $this.Events.Add("write:${Key}=$Value")
        $this.Values[$Key] = $Value
        if ($Key -eq 'Enable' -and $Value -eq 1) { $this.OnAt = [Diagnostics.Stopwatch]::GetTimestamp() }
        if ($Key -eq 'Enable' -and $Value -eq 0 -and $this.OnAt -gt 0 -and $this.OffAt -eq 0) {
            $this.OffAt = [Diagnostics.Stopwatch]::GetTimestamp()
        }
        if ($this.Fault -eq 'parameter' -and $Key -eq 'Xi') { throw 'injected parameter write failure' }
        if ($this.Fault -eq 'enable_readback' -and $Key -eq 'Enable' -and $Value -eq 1) { throw 'injected enable readback failure' }
    }
    $rig | Add-Member ScriptMethod Read { param($Key); return $this.Values[$Key] }
    $rig | Add-Member ScriptMethod StartRecorders {
        param($Directory, $Stem)
        $this.Events.Add('record_start:' + $Stem)
        $this.Recording = $true
        if ($this.Fault -eq 'partial_start') { throw 'injected second recorder start failure' }
    }
    $rig | Add-Member ScriptMethod CheckRecording {
        if ($this.Fault -eq 'recording') { throw 'injected recording loss' }
    }
    $rig | Add-Member ScriptMethod StopRecorders {
        $this.Events.Add('record_stop')
        $this.Recording = $false
        if ($this.Fault -eq 'stop') { throw 'injected stop failure' }
    }
    $rig | Add-Member ScriptMethod VerifyFiles {
        $this.Events.Add('verify_files')
        if ($this.Fault -eq 'save') { throw 'injected save failure' }
        return 'fake_meas1abc.mf4', 'fake_meas2abc.mf4'
    }
    return $rig
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('ControlDeskSweepTests_' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $root)
try {
    # Exercise the production StartRecorders method, including decimal points in the stem.
    $tokens = $null; $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $ProjectRoot 'core\run_parameter_sweep.ps1'), [ref]$tokens, [ref]$parseErrors)
    Assert ($parseErrors.Count -eq 0) 'Sweep script must parse.'
    $startCommand = $ast.Find({param($node)
        $node -is [Management.Automation.Language.CommandAst] -and
        $node.GetCommandName() -eq 'Add-Member' -and
        $node.CommandElements[2].Extent.Text -eq 'StartRecorders'
    }, $true)
    $recorder = [pscustomobject]@{BaseFileName=''; Started=$false}
    $recorder | Add-Member ScriptMethod Start {
        param($First, $Second)
        if ([IO.Path]::GetExtension($this.BaseFileName) -ne '.mf4') {
            throw 'Unsupported recording extension.'
        }
        $this.Started = $true
    }
    $entry = [pscustomobject]@{Group=[pscustomobject]@{Name='meas1abc'}; Recorder=$recorder; ExpectedFile=''}
    $startRig = [pscustomobject]@{Recorders=@($entry)}
    $startRig | Add-Member ScriptMethod CheckRecording {}
    $startRig | Add-Member ScriptMethod StartRecorders $startCommand.CommandElements[-1].ScriptBlock.GetScriptBlock()
    $startRig.StartRecorders($root, 'xi_0.8+84.9_84.9')
    Assert $recorder.Started 'Recorder must start with a supported extension.'
    Assert ($entry.ExpectedFile -eq (Join-Path $root 'xi_0.8+84.9_84.9+meas1abc.mf4')) 'Case filename changed.'
    $firstName = $recorder.BaseFileName
    $startRig.StartRecorders($root, 'xi_0.8+84.9_84.9')
    Assert ($firstName -ne $recorder.BaseFileName) 'Source recording names must remain unique.'
    Write-Output 'PASS: production recorder start uses unique .mf4 names and preserves case filenames.'
    # ControlDesk saves into the experiment folder, not the requested case directory.
    $source = Join-Path $root 'recorded.mf4'
    $target = Join-Path $root 'case.mf4'
    $bytes = New-Object byte[] 128
    [Text.Encoding]::ASCII.GetBytes('MDF     ').CopyTo($bytes, 0)
    [IO.File]::WriteAllBytes($source, $bytes)
    $recorder = [pscustomobject]@{State=0; LastRecordedFiles=@($source)}
    Save-SweepRecording $recorder $target -TimeoutSeconds 0
    Assert ((Get-FileHash $source).Hash -eq (Get-FileHash $target).Hash) 'Recording copy differs.'
    Assert (Test-Path $source) 'Original recording must be preserved.'
    $failed = $false
    try { Save-SweepRecording $recorder $target -TimeoutSeconds 0 } catch { $failed = $true }
    Assert $failed 'Existing waveform must not be overwritten.'
    $recorder.LastRecordedFiles = @()
    $failed = $false
    try { Save-SweepRecording $recorder $target -TimeoutSeconds 0 }
    catch { $failed = $_.Exception.Message -like '*LastRecordedFiles=*' }
    Assert $failed 'Missing recording must include diagnostic details.'
    Write-Output 'PASS: recording relocation, original preservation, overwrite refusal and timeout diagnostics.'
    $csv = Join-Path $ProjectRoot 'config\sweep_cases.csv'
    $cases = @(Import-SweepCases $csv)
    Assert ($cases.Count -eq 15) 'Expected 15 cases.'
    foreach ($pair in @(@(100,1), @(120,8), @(140,4), @(160,2))) {
        Assert (@(Import-SweepCases $csv $pair[0]).Count -eq $pair[1]) 'Incorrect VL grouping.'
    }
    $a = @($cases | Where-Object Id -like 'A*')
    for ($i=1; $i -lt $a.Count; $i++) {
        Assert ([math]::Abs($a[$i].Xi-$a[$i-1].Xi-0.01) -lt 1e-12) 'Control A step must be 0.01.'
    }
    Assert ((Get-SweepStem 0.4 60 60) -eq 'xi_0.4+84.9_84.9') 'Incorrect filename/scaling.'
    $culture = [Threading.Thread]::CurrentThread.CurrentCulture
    try {
        [Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::GetCultureInfo('de-DE')
        Assert ((Get-SweepStem 0.51 60 60) -eq 'xi_0.51+84.9_84.9') 'Filename must not depend on decimal locale.'
    } finally { [Threading.Thread]::CurrentThread.CurrentCulture = $culture }
    $rig = New-FakeRig
    $directory = Join-Path $root 'success'
    [void](New-Item -ItemType Directory -Path $directory)
    $result = Invoke-SweepCase $rig $cases[0] $directory -Duration 0.03 -OffSeconds 0.04
    Assert ($result.Status -eq 'complete' -and $rig.Values.Enable -eq 0 -and -not $rig.Recording) 'Incorrect success final state.'
    $events = $rig.Events.ToArray()
    $on = [array]::IndexOf($events, 'write:Enable=1')
    $off = [array]::IndexOf($events, 'write:Enable=0', $on+1)
    $stop = [array]::IndexOf($events, 'record_stop')
    $start = [array]::FindIndex($events, [Predicate[string]]{param($s) $s.StartsWith('record_start:')})
    Assert ($start -lt $on -and $on -lt $off -and $off -lt $stop) 'Incorrect recorder/enable ordering.'
    Assert (([Diagnostics.Stopwatch]::GetTimestamp()-$rig.OffAt)/[Diagnostics.Stopwatch]::Frequency -ge 0.04) 'Off wait too short.'
    foreach ($fault in @('parameter','enable_readback','partial_start','recording','stop','save')) {
        $rig = New-FakeRig $fault
        $directory = Join-Path $root $fault
        [void](New-Item -ItemType Directory -Path $directory)
        $failed = $false
        try { Invoke-SweepCase $rig $cases[0] $directory -Duration 0.01 -OffSeconds 0 | Out-Null }
        catch { $failed = $true }
        Assert $failed ('Failure not propagated: ' + $fault)
        Assert ($rig.Values.Enable -eq 0 -and -not $rig.Recording) ('Fault cleanup failed: ' + $fault)
        $log = Get-Content (Join-Path $directory 'case.json') -Raw | ConvertFrom-Json
        Assert ($log.Status -eq 'failed') ('Fault logged as success: ' + $fault)
    }
    foreach ($fault in @('', 'prepare', 'tick', 'frozen', 'export', 'cleanup')) {
        $rig = New-FakeRig
        $scope = [pscustomobject]@{Rig=$rig; Fault=$fault}
        $scope | Add-Member ScriptMethod Prepare {
            param($Directory, $Context)
            Assert ($this.Rig.Values.Enable -eq 0) 'Scope prepare must run with Enable=0.'
            if ($this.Fault -eq 'prepare') { throw 'injected prepare failure' }
        }
        $scope | Add-Member ScriptMethod Tick {
            param($Elapsed)
            if ($this.Fault -eq 'tick') { throw 'injected acquisition failure' }
        }
        $scope | Add-Member ScriptMethod AssertFrozen {
            if ($this.Fault -eq 'frozen') { throw 'injected missing frozen data' }
        }
        $scope | Add-Member ScriptMethod Export {
            Assert ($this.Rig.Values.Enable -eq 0 -and -not $this.Rig.Recording) 'Export must follow disable and recorder stop.'
            $this.Rig.Events.Add('scope_export')
            if ($this.Fault -eq 'export') { throw 'injected export failure' }
            return 'fake_manifest.json'
        }
        $scope | Add-Member ScriptMethod Cleanup {
            Assert ($this.Rig.Values.Enable -eq 0) 'Scope cleanup must follow disable.'
            if ($this.Fault -eq 'cleanup') { throw 'injected scope cleanup failure' }
        }
        $directory = Join-Path $root ('scope_' + $fault)
        [void](New-Item -ItemType Directory -Path $directory)
        $failed = $false
        try { $result = Invoke-SweepCase $rig $cases[0] $directory -Duration 0.03 -OffSeconds 0 -Scope $scope }
        catch { $failed = $true }
        Assert ($failed -eq ($fault -ne '')) ('Scope failure handling: ' + $fault)
        Assert ($rig.Values.Enable -eq 0 -and -not $rig.Recording) ('Scope final state: ' + $fault)
        if (-not $failed) { Assert ($result.ScopeManifest -eq 'fake_manifest.json') 'Missing manifest.' }
    }
    $stopFile = Join-Path $root 'stop.json'
    [IO.File]::WriteAllText($stopFile, '{}')
    $rig = New-FakeRig
    $directory = Join-Path $root 'user_stop'
    [void](New-Item -ItemType Directory -Path $directory)
    $failed = $false
    try { $stopped = Invoke-SweepCase $rig $cases[0] $directory -Duration 0.03 -StopFile $stopFile }
    catch { $failed = $true }
    Assert (-not $failed) 'A user stop is an outcome, not a fault.'
    Assert ('write:Enable=1' -notin $rig.Events) 'Stop request must prevent enable.'
    Assert ($stopped.Status -eq 'stopped') 'A stop before enabling must be reported as stopped.'
    # Regression: a stop that lands while the rig is enabled must not destroy the
    # case. Exporting a frozen waveform is uninterruptible, so the case still ends
    # with its manifest and reason, and the batch stops afterwards.
    $rig = New-FakeRig
    $directory = Join-Path $root 'stop_during_export'
    [void](New-Item -ItemType Directory -Path $directory)
    $stopFile2 = Join-Path $directory 'stop.json'
    $scope2 = [pscustomobject]@{Rig=$rig; Stopped=$false; ExportCount=0; StopFile=$stopFile2}
    $scope2 | Add-Member ScriptMethod SetUp { $this.Stopped = $true }
    $scope2 | Add-Member ScriptMethod Prepare { param($Directory, $Context) }
    $scope2 | Add-Member ScriptMethod Tick {
        param([double]$Elapsed)
        if ($Elapsed -gt 0.001 -and -not $this.Stopped) {
            [IO.File]::WriteAllText($this.StopFile, '{}')
            $this.SetUp()
        }
    }
    $scope2 | Add-Member ScriptMethod AssertFrozen {}
    $scope2 | Add-Member ScriptMethod Export {
        $this.ExportCount++
        Assert ($this.Rig.Values.Enable -eq 0 -and -not $this.Rig.Recording) 'Export must follow disable and recorder stop.'
        return 'fake_manifest.json'
    }
    $scope2 | Add-Member ScriptMethod Cleanup {
        Remove-Item -LiteralPath $this.StopFile -Force -ErrorAction SilentlyContinue
    }
    $failed = $false
    try { $result = Invoke-SweepCase $rig $cases[0] $directory -Duration 0.05 -OffSeconds 0 -Scope $scope2 -StopFile $stopFile2 }
    catch { $failed = $true }
    Assert (-not $failed) 'A stop during export must not abort the case.'
    Assert ($scope2.ExportCount -eq 1 -and $result.ScopeManifest -eq 'fake_manifest.json') 'A stop during export must not discard the frozen waveform.'
    Assert ($result.Status -eq 'stopped') 'A stopped case must keep its own reason, not report a fault.'
    Assert ($rig.Values.Enable -eq 0 -and -not $rig.Recording) 'Stopped case left the rig enabled.'
    Write-Output 'PASS: cases/locale/timing; six recorder faults; scope prepare/acquire/freeze/export/cleanup faults; user stop; stop during export.'
} finally { Remove-Item -LiteralPath $root -Recurse -Force }
