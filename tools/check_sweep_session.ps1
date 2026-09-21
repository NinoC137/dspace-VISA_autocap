$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$out = Join-Path $ProjectRoot 'docs\reference\sweep_check.txt'
function Read-State {
    $a = [Runtime.InteropServices.Marshal]::GetActiveObject('ControlDeskNG.Application')
    $p = $a.ActiveExperiment.Platforms.Item('Platform')
    $values = @{}
    foreach ($name in @('Enable1','Constant3','ud_ref6','ud_ref7','phi2_deg')) {
        $values[$name] = $p.ActiveVariableDescription.Variables.Item('Model Root/' + $name + '/Value').ValueConverted
    }
    return [ordered]@{Calibration=$p.CalibrationState; Measurement=$p.MeasurementState
        IsMeasuring=$a.MeasurementDataManagement.IsMeasuring
        RecorderCount=$a.MeasurementDataManagement.Recorders.Count; Values=$values}
}
try {
    $before = Read-State
    & (Join-Path $ProjectRoot 'core\run_parameter_sweep.ps1') -Mode Check -LowVoltage 120 | Out-File $out -Encoding UTF8
    $after = Read-State
    if (($before | ConvertTo-Json -Depth 5 -Compress) -ne ($after | ConvertTo-Json -Depth 5 -Compress)) {
        throw 'State changed during check.'
    }
    @('STATE UNCHANGED', ($after | ConvertTo-Json -Depth 5)) | Out-File $out -Append -Encoding UTF8
} catch { @($_.Exception.ToString(), $_.ScriptStackTrace, $_.InvocationInfo.PositionMessage) | Out-File $out -Append -Encoding UTF8; exit 1 }
