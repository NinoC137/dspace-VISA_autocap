param([switch]$Child)
$ErrorActionPreference = 'Stop'
$out = Join-Path $PSScriptRoot 'dataset_diagnostic.txt'
if ($Child) {
    try {
        $a = [Runtime.InteropServices.Marshal]::GetActiveObject('ControlDeskNG.Application')
        $p = $a.ActiveExperiment.Platforms.Item('Platform')
        $d = $p.ActiveVariableDescription
        & (Join-Path $PSScriptRoot 'dataset_fix_validation_20260918\check_sweep_session.ps1')
        & {
            'PROJECT: ' + $a.ActiveProject.Name
            'EXPERIMENT: ' + $a.ActiveExperiment.Name
            $assembly = [Reflection.Assembly]::LoadFrom('C:\Program Files\dSPACE ControlDesk 2023-A\Main\bin\AutomationAssemblies\dSPACE.InterfaceDefinitions.PlatformManagement.Automation24.dll')
            foreach ($t in $assembly.GetTypes()) {
                if ($t.IsEnum -and $t.Name -match 'OnlineCalibrationBehavior|PlatformType|Capabilities|CalibrationState') {
                    $t.FullName
                    foreach ($n in [Enum]::GetNames($t)) { $n + '=' + [int][Enum]::Parse($t, $n) }
                }
            }
            $p | Select-Object Name,ConnectionState,CalibrationState,MeasurementState | Format-List
            'DATASET MEMBERS'
            Get-Member -InputObject $d.DataSets | Out-String -Width 240
            'DATASET PROPERTIES'
            [pscustomobject]@{Count=$d.DataSets.Count; WorkingIsNull=($null -eq $d.DataSets.WorkingDataSet); ActiveIsNull=($null -eq $d.DataSets.ActiveDataSet); ReferenceIsNull=($null -eq $d.DataSets.ReferenceDataSet); PlatformType=$p.Type; Capabilities=$p.Capabilities} | Format-List
            'GENERAL SETTINGS'
            $p.GeneralSettings | Format-List * | Out-String -Width 240
            'DATASET ITEMS'
            foreach ($s in $d.DataSets) { $s | Format-List * | Out-String -Width 240 }
            'PLATFORM MEMBERS'
            Get-Member -InputObject $p | Out-String -Width 240
            'ENABLE MEMBERS'
            $v = $d.Variables.Item('Model Root/Enable1/Value')
            Get-Member -InputObject $v | Out-String -Width 240
            'CURRENT PARAMETERS'
            foreach ($n in @('Enable1','Constant3','ud_ref6','ud_ref7','phi2_deg')) {
                $v = $d.Variables.Item('Model Root/' + $n + '/Value')
                [pscustomobject]@{Name=$n; Value=$v.ValueConverted; Readable=$v.IsReadable; Writable=$v.IsWritable} | Format-List
            }
        } | Out-File $out -Encoding UTF8 -Width 240
    } catch { $_ | Out-String | Out-File $out -Append -Encoding UTF8 }
    'done' | Set-Content ($out + '.done')
    exit
}
$task = 'ReadOnlyDataset_' + [guid]::NewGuid().ToString('N')
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Child')
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
try {
    Remove-Item ($out + '.done') -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $task -Action $action -Principal $principal | Out-Null
    Start-ScheduledTask -TaskName $task
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path ($out + '.done'))) {
        if ($watch.Elapsed.TotalSeconds -gt 45) { throw 'Read-only diagnostic timed out.' }
        Start-Sleep -Milliseconds 200
    }
    Get-Content $out
} finally { Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue }
