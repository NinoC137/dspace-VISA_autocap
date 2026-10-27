# Offline only: fake rig and scope; no COM, sockets, VISA or instrument process.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $root 'core\sweep_core.ps1')
$cases = @(Import-SweepCases (Join-Path $PSScriptRoot 'cases.csv') 120)
if ($cases.Count -ne 111) { throw 'Expected 111 cases.' }
$state = [pscustomobject]@{Enable=0.0; Xi=0.0; Phi=0.0; Ud6=0.0; Ud7=0.0}
$rig = [pscustomobject]@{State=$state; EnableCount=0}
$rig | Add-Member ScriptMethod AssertReady { if ($this.State.Enable -ne 0) { throw 'Already enabled.' } }
$rig | Add-Member ScriptMethod Read { param($key) return [double]$this.State.$key }
$rig | Add-Member ScriptMethod Write {
    param($key, [double]$value)
    $this.State.$key = $value
    if ($key -eq 'Enable' -and $value -eq 1) { $this.EnableCount++ }
}
$rig | Add-Member ScriptMethod StartRecorders { param($directory,$stem) }
$rig | Add-Member ScriptMethod CheckRecording { }
$rig | Add-Member ScriptMethod StopRecorders { }
$rig | Add-Member ScriptMethod VerifyFiles { return @() }
$rig | Add-Member ScriptMethod ReadObservations {
    return [pscustomobject]@{phaseCommandDeg=$this.State.Phi; xiReadback=$this.State.Xi}
}
$scope = [pscustomobject]@{Stopped=$false; FailExport=$false}
$scope | Add-Member ScriptMethod Prepare { param($directory,$context) }
$scope | Add-Member ScriptMethod Tick { param($elapsed) }
$scope | Add-Member ScriptMethod AssertFrozen { }
$scope | Add-Member ScriptMethod Export {
    if ($this.FailExport) { throw 'Injected export failure.' }
    return 'fake-manifest.json'
}
$scope | Add-Member ScriptMethod Cleanup { }
$folder = Join-Path ([IO.Path]::GetTempPath()) ('phase5_fake_' + [guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory $folder)
try {
    foreach ($case in $cases) {
        $directory = Join-Path $folder $case.Id
        [void](New-Item -ItemType Directory $directory)
        $result = Invoke-SweepCase $rig $case $directory -Duration .01 -OffSeconds .001 -Scope $scope
        $saved = Get-Content (Join-Path $directory 'case.json') -Raw | ConvertFrom-Json
        if ($result.Status -ne 'complete' -or $state.Enable -ne 0 -or
            $saved.Readback.Phi -ne $case.Phi -or $saved.Readback.Xi -ne $case.Xi -or
            $saved.Observations.phaseCommandDeg -ne $case.Phi -or
            $saved.Observations.xiReadback -ne $case.Xi -or -not $saved.ScopeManifest) {
            throw ('Angle/xi/manifest logging failed: ' + $case.Id)
        }
    }
    if ($rig.EnableCount -ne 111) { throw 'A case was skipped or repeated.' }
    $scope.FailExport = $true
    $directory = Join-Path $folder 'export_failure'
    [void](New-Item -ItemType Directory $directory)
    $failed = $false
    try { [void](Invoke-SweepCase $rig $cases[0] $directory -Duration .01 -OffSeconds .001 -Scope $scope) }
    catch { $failed = $_.Exception.Message -match 'Injected export failure' }
    $saved = Get-Content (Join-Path $directory 'case.json') -Raw | ConvertFrom-Json
    if (-not $failed -or $state.Enable -ne 0 -or $saved.Status -ne 'failed') {
        throw 'Failure did not disable Enable1 and preserve case status.'
    }
    Write-Output 'OFFLINE SMOKE OK: 111 case angle/xi logs; export failure disables Enable1.'
} finally { Remove-Item -LiteralPath $folder -Recurse -Force }
