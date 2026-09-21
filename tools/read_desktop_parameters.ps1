# Windows PowerShell 5.1. Attach to the current user's running ControlDesk only.
# No COM property setters, project activation, calibration changes or target commands.
[CmdletBinding()]
param([string]$OutputDirectory = '')

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2.0
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $PSScriptRoot) 'parameter_snapshots'
}

function Read-Property($Object, [string]$Name) {
    if ($null -eq $Object) { throw "Cannot read $Name from a null object." }
    # Invoke the COM getter explicitly so errors do not silently become empty values.
    $result = $Object.GetType().InvokeMember(
        $Name, [Reflection.BindingFlags]::GetProperty, $null, $Object, $null)
    return ,$result
}

function Read-Value($Object, [string]$Property) {
    try {
        $value = Read-Property $Object $Property
        if ($null -eq $value -or $value -is [DBNull]) {
            return [pscustomobject]@{ValueJson=''; Status='unavailable'; Error='COM returned no value'}
        }
        if ([Runtime.InteropServices.Marshal]::IsComObject($value)) {
            throw 'Unexpected COM object instead of a scalar or array value.'
        }
        return [pscustomobject]@{
            ValueJson=(ConvertTo-Json -InputObject $value -Depth 30 -Compress)
            Status='read_ok'; Error=''
        }
    } catch {
        return [pscustomobject]@{ValueJson=''; Status='read_error'; Error=$_.Exception.Message}
    }
}

function Get-PlatformState($Platform) {
    [pscustomobject]@{
        Name=[string]$Platform.Name
        ConnectionState=[int]$Platform.ConnectionState
        CalibrationState=[int]$Platform.CalibrationState
        MeasurementState=[int]$Platform.MeasurementState
    }
}

function Get-UIEntry($Object, $Layout, $Instrument, $RowIndex) {
    $value = Read-Value $Object 'Value'
    $identifier = $Object.MainVariable
    $path = ''; $writable = $null
    if ($null -ne $identifier) {
        $path = [string]$identifier.Path
    }
    $connection = $Object.Connection
    if ($null -ne $connection -and $null -ne $connection.Variable) {
        $writable = [bool]$connection.Variable.IsWritable
    }
    [pscustomobject]@{
        Layout=[string]$Layout.Name; Instrument=[string]$Instrument.Name
        InstrumentType=[string]$Instrument.TypeName; RowIndex=$RowIndex
        Path=$path; IsWritable=$writable; IsMainVariableValid=[bool]$Object.IsMainVariableValid
        ValueJson=$value.ValueJson; ReadStatus=$value.Status; Error=$value.Error
        ValueSource='UI Value getter; display/cache, not proof of a fresh target read'
        ReadAt=(Get-Date).ToString('o')
    }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
$runDirectory = Join-Path $OutputDirectory ($stamp + '_' + $PID)
[void](New-Item -ItemType Directory -Path $runDirectory)
$parameters = [Collections.Generic.List[object]]::new()
$variables = [Collections.Generic.List[object]]::new()
$ui = [Collections.Generic.List[object]]::new()
$platforms = [Collections.Generic.List[object]]::new()
$issues = [Collections.Generic.List[string]]::new()
$snapshot = [ordered]@{
    StartedAt=(Get-Date).ToString('o'); FinishedAt=$null
    Computer=$env:COMPUTERNAME; User=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    SessionId=[Diagnostics.Process]::GetCurrentProcess().SessionId
    Project=''; Experiment=''; ControlDeskVersion=''; Status='running'
    Semantics='Sequential read-only snapshot, not atomic. No target refresh or state change requested. Null is unavailable, never zero.'
    Parameters=$parameters; Variables=$variables; UIEntries=$ui; Platforms=$platforms; Issues=$issues
}
$exitCode = 1
try {
    $app = [Runtime.InteropServices.Marshal]::GetActiveObject('ControlDeskNG.Application')
    if ($null -eq $app.ActiveExperiment) { throw 'No active ControlDesk experiment.' }
    $snapshot.Project = [string]$app.ActiveProject.Name
    $snapshot.Experiment = [string]$app.ActiveExperiment.Name
    $snapshot.ControlDeskVersion = [string]$app.Version
    foreach ($platform in $app.ActiveExperiment.Platforms) {
        $before = Get-PlatformState $platform
        $description = $platform.ActiveVariableDescription
        if ($null -eq $description) {
            $issues.Add('No active variable description: ' + $platform.Name)
            continue
        }
        $activeDataSet = $description.DataSets.ActiveDataSet
        $dataSetName = if ($null -eq $activeDataSet) { '' } else { [string]$activeDataSet.Name }
        $count = 0
        foreach ($variable in $description.Variables) {
            $count++
            $path = [string]$variable.Identifier.Path
            $metadata = [pscustomobject]@{
                Platform=[string]$platform.Name; Path=$path
                UniqueName=[string]$variable.Identifier.UniqueName
                Name=[string]$variable.Name; Unit=[string]$variable.Unit
                Type=[int]$variable.Type; IsReadable=[bool]$variable.IsReadable
                IsWritable=[bool]$variable.IsWritable
            }
            $variables.Add($metadata)
            if (-not $metadata.IsWritable) { continue }
            $value = Read-Value $variable 'ValueConverted'
            $parameters.Add([pscustomobject]@{
                Platform=$metadata.Platform; Path=$path; UniqueName=$metadata.UniqueName
                Name=$metadata.Name; Unit=$metadata.Unit; Type=$metadata.Type
                IsReadable=$metadata.IsReadable; IsWritable=$metadata.IsWritable
                ValueJson=$value.ValueJson; ReadStatus=$value.Status; Error=$value.Error
                ActiveDataSet=$dataSetName
                ValueSource='ControlDesk ValueConverted getter; current source unchanged'
                ConnectionState=$before.ConnectionState; CalibrationState=$before.CalibrationState
                ReadAt=(Get-Date).ToString('o')
            })
        }
        $after = Get-PlatformState $platform
        $platforms.Add([pscustomobject]@{
            Before=$before; After=$after; ActiveDataSet=$dataSetName
            VariableDescription=[string]$description.FullName
            ExpectedVariableCount=[int]$description.Variables.Count; EnumeratedVariableCount=$count
        })
        if ($count -ne [int]$description.Variables.Count) { $issues.Add('Variable count changed or enumeration incomplete: ' + $platform.Name) }
        if (($before | ConvertTo-Json -Compress) -ne ($after | ConvertTo-Json -Compress)) {
            $issues.Add('Platform state changed during reading: ' + $platform.Name)
        }
    }
    foreach ($layout in $app.LayoutManagement.Layouts) {
        foreach ($instrument in $layout.Instruments) {
            try {
                if ($instrument.TypeName -eq 'Variable Array') {
                    $index = 0
                    foreach ($row in $instrument.Rows) {
                        $ui.Add((Get-UIEntry $row $layout $instrument $index))
                        $index++
                    }
                } elseif ($instrument.TypeName -eq 'Display') {
                    $ui.Add((Get-UIEntry $instrument $layout $instrument -1))
                } elseif ($instrument.TypeName -ne 'Time Plotter') {
                    $issues.Add('UI instrument not traversed: ' + $layout.Name + '/' + $instrument.Name + ' (' + $instrument.TypeName + ')')
                }
            } catch { $issues.Add('UI read failed: ' + $instrument.Name + ': ' + $_.Exception.Message) }
        }
    }
    $snapshot.Status = 'complete'
    $exitCode = 0
    if ($parameters.Count -eq 0 -or @($parameters | Where-Object ReadStatus -ne 'read_ok').Count -gt 0 -or $issues.Count -gt 0) {
        $snapshot.Status = 'partial_values_or_coverage'
        $exitCode = 2
    }
} catch {
    $snapshot.Status = 'failed'
    $issues.Add($_.Exception.ToString())
    $issues.Add('Run in the same signed-in Windows desktop session/user/elevation as ControlDesk. SSH has a separate COM session.')
} finally {
    $snapshot.FinishedAt = (Get-Date).ToString('o')
    $parameters | Export-Csv (Join-Path $runDirectory 'parameters.csv') -NoTypeInformation -Encoding UTF8
    $variables | Export-Csv (Join-Path $runDirectory 'variables.csv') -NoTypeInformation -Encoding UTF8
    $ui | Export-Csv (Join-Path $runDirectory 'ui_parameters.csv') -NoTypeInformation -Encoding UTF8
    $ui | Where-Object IsWritable -eq $true | Export-Csv (Join-Path $runDirectory 'ui_settings.csv') -NoTypeInformation -Encoding UTF8
    ConvertTo-Json -InputObject $snapshot -Depth 35 | Set-Content (Join-Path $runDirectory 'snapshot.json') -Encoding UTF8
    $summary = @(
        'Status: ' + $snapshot.Status
        'Project: ' + $snapshot.Project + ' / ' + $snapshot.Experiment
        'Variables: ' + $variables.Count
        'Writable parameters: ' + $parameters.Count
        'Parameter values returned: ' + @($parameters | Where-Object ReadStatus -eq 'read_ok').Count
        'UI entries: ' + $ui.Count
        'UI values returned: ' + @($ui | Where-Object ReadStatus -eq 'read_ok').Count
        'Output: ' + $runDirectory
        'Exit codes: 0=all parameter values read, 2=partial/unavailable, 1=attachment/enumeration failed'
    )
    $summary | Set-Content (Join-Path $runDirectory 'summary.txt') -Encoding UTF8
    $summary | Write-Output
}
exit $exitCode
