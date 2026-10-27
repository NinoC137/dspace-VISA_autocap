# Pure fake-rig test. Never connects to ControlDesk or the oscilloscope.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot 'sweep_core_dynamic.ps1')
$trajectory = Get-Content (Join-Path $PSScriptRoot 'trajectory.json') -Raw | ConvertFrom-Json
$state = [pscustomobject]@{Enable=0.0; Xi=0.4; Phi=90.0; Ud6=60.0; Ud7=60.0}
$rig = [pscustomobject]@{State=$state; FailOnHighXi=$false}
$rig | Add-Member ScriptMethod AssertReady { if ($this.State.Enable -ne 0) { throw 'fake rig enabled' } }
$rig | Add-Member ScriptMethod Read { param($key) return [double]$this.State.$key }
$rig | Add-Member ScriptMethod Write { param($key, [double]$value) $this.State.$key = $value }
$rig | Add-Member ScriptMethod StartRecorders { param($directory, $stem) }
$rig | Add-Member ScriptMethod CheckRecording { }
$rig | Add-Member ScriptMethod StopRecorders { }
$rig | Add-Member ScriptMethod VerifyFiles { return @() }
$rig | Add-Member ScriptMethod ReadObservations {
    $prediction = if ($this.State.Xi -lt 0.5) { 95.2 } elseif ($this.State.Xi -lt 0.7) { 159.4 } else { 103.1 }
    $valid = if ($this.FailOnHighXi -and $this.State.Xi -ge .8) { 0 } else { 1 }
    return [pscustomobject]@{phiOptDeg=$prediction; valid=$valid; status=0}
}
$fakeScope = [pscustomobject]@{Stopped=$false; Asserting=0; Updated=$false}
$fakeScope | Add-Member ScriptMethod Prepare { param($directory, $context) }
$fakeScope | Add-Member ScriptMethod Tick { param($elapsed) }
$fakeScope | Add-Member ScriptMethod AssertAcquiring { $this.Asserting++ }
$fakeScope | Add-Member ScriptMethod AssertFrozen { }
$fakeScope | Add-Member ScriptMethod UpdateContext {
    param($context)
    if ($context.Events.Count -lt 3) { throw 'Fake scope did not receive completed event history.' }
    $this.Updated = $true
}
$fakeScope | Add-Member ScriptMethod Export { return 'fake-manifest' }
$fakeScope | Add-Member ScriptMethod Cleanup { }
$folder = Join-Path ([IO.Path]::GetTempPath()) ('xi_switch_fake_' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory $folder)
try {
    foreach ($policy in @('fixed90', 'fixed180', 'adaptive')) {
        $state.Enable = 0; $state.Xi = .4; $state.Phi = 90
        $phi = if ($policy -eq 'fixed180') { 180 } else { 90 }
        $case = [pscustomobject]@{Id=('XSW_' + $policy); VL=120; Xi=.4; Ud6=60; Ud7=60; Phi=$phi; Note=$policy}
        $subfolder = Join-Path $folder $policy
        [void](New-Item -ItemType Directory $subfolder)
        $scopeForCase = if ($policy -eq 'fixed90') { $fakeScope } else { $null }
        $result = Invoke-SweepCase $rig $case $subfolder -Duration 9.6 -OffSeconds .001 `
            -Trajectory $trajectory -Scope $scopeForCase
        $switches = @($result.Events | Where-Object action -eq 'xi_switch')
        $updates = @($result.Events | Where-Object action -eq 'phi_update')
        $wantUpdates = if ($policy -eq 'adaptive') { 3 } else { 0 }
        $wantPhi = if ($policy -eq 'adaptive') { 103 } else { $phi }
        if ($result.Status -ne 'complete' -or $state.Enable -ne 0 -or
            $switches.Count -ne 2 -or $updates.Count -ne $wantUpdates -or
            $state.Xi -ne .8 -or $state.Phi -ne $wantPhi) {
            throw ('Fake-rig dynamic sequence failed: ' + $policy)
        }
        if ($policy -eq 'fixed90' -and ($fakeScope.Asserting -ne 2 -or -not $fakeScope.Updated)) {
            throw 'Scope acquisition interlock/context update was not exercised for both switches.'
        }
    }
    $state.Enable = 0; $state.Xi = .4; $state.Phi = 90
    $steadyCase = [pscustomobject]@{Id='XSW_STEADY_BEST'; VL=120; Xi=.515; Ud6=60; Ud7=60; Phi=159; Note='steady_best'}
    $steadyFolder = Join-Path $folder 'steady_best'
    [void](New-Item -ItemType Directory $steadyFolder)
    $steady = Invoke-SweepCase $rig $steadyCase $steadyFolder -Duration 1.5 -OffSeconds .001 -Trajectory $null
    if ($steady.Status -ne 'complete' -or $state.Enable -ne 0 -or
        $state.Xi -ne .515 -or $state.Phi -ne 159 -or
        @($steady.Events | Where-Object action -eq 'xi_switch').Count -ne 0 -or
        @($steady.Events | Where-Object action -eq 'phi_update').Count -ne 0) {
        throw 'Fake-rig fixed-xi steady sequence failed.'
    }
    $rig.FailOnHighXi = $true
    $state.Enable = 0; $state.Xi = .4; $state.Phi = 90
    $failureFolder = Join-Path $folder 'invalid_prediction'
    [void](New-Item -ItemType Directory $failureFolder)
    $case = [pscustomobject]@{Id='XSW_INVALID'; VL=120; Xi=.4; Ud6=60; Ud7=60; Phi=90; Note='adaptive'}
    $failedAsExpected = $false
    try { [void](Invoke-SweepCase $rig $case $failureFolder -Duration 9.6 -OffSeconds .001 -Trajectory $trajectory) }
    catch { $failedAsExpected = $_.Exception.Message -match 'predicted angle is invalid' }
    if (-not $failedAsExpected -or $state.Enable -ne 0) { throw 'Invalid-prediction shutdown failed.' }
    Write-Output 'OFFLINE SMOKE OK: 90, 180, adaptive and fixed-xi steady; invalid prediction disables Enable1.'
} finally {
    Remove-Item -LiteralPath $folder -Recurse -Force
}
