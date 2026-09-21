$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$out = Join-Path $ProjectRoot 'docs\reference\recording_api.txt'
function Describe($Name, $Object) {
    $Name | Out-File $out -Append -Encoding utf8
    if ($null -ne $Object) {
        (Get-Member -InputObject $Object | Out-String -Width 260) | Out-File $out -Append -Encoding utf8
    }
}
try {
    $a = [Runtime.InteropServices.Marshal]::GetActiveObject('ControlDeskNG.Application')
    @($a.ActiveProject.Name, $a.ActiveExperiment.Name) | Out-File $out -Encoding utf8
    $m = $a.MeasurementDataManagement
    Describe 'MDM' $m
    Describe 'RECORDER' $m.MainRecorder
    Describe 'CONFIGURATION' $m.MeasurementConfiguration
    Describe 'BUFFER' $m.MeasurementConfiguration.Buffer
    Describe 'STOP CONDITION' $m.MainRecorder.StopCondition
    $inventory = @()
    foreach ($l in $a.LayoutManagement.Layouts) {
        foreach ($i in $l.Instruments) {
            if ($i.TypeName -ne 'Time Plotter') { continue }
            ('PLOTTER: ' + $l.Name + '/' + $i.Name) | Out-File $out -Append -Encoding utf8
            foreach ($p in $i.Plots) {
                Describe 'PLOT' $p
                foreach ($y in $p.YAxes) {
                    foreach ($s in $y.Signals) {
                        $inventory += [pscustomobject]@{
                            Layout=$l.Name; Instrument=$i.Name; Plot=$p.Name
                            SignalName=$s.Name; Path=$s.MainVariable.Path
                            ConnectionPath=$s.Connection.OriginConnectionPath
                            RasterName=$s.Connection.RasterName
                        }
                    }
                }
            }
        }
    }
    $inventory | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $ProjectRoot 'docs\reference\plot_inventory.json') -Encoding UTF8
    Describe 'MEASUREMENT SIGNALS' $m.MeasurementConfiguration.Signals
    foreach ($s in $m.MeasurementConfiguration.Signals) {
        ($s | Format-List * | Out-String -Width 220) | Out-File $out -Append -Encoding utf8
        break
    }
} catch { $_.Exception.ToString() | Out-File $out -Append -Encoding utf8; exit 1 }
