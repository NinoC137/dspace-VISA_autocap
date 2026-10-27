# Dedicated recorder groups need not be attached to a visible plotter.
# These functions perform COM operations only when explicitly invoked by Run/Check.
function Test-SweepDirectGroup($Group) {
    return [bool]($Group.PSObject.Properties['Source'] -and $Group.Source -eq 'Direct')
}

function Test-SweepDirectSignals($Description, $Group) {
    foreach ($definition in $Group.Signals) {
        if ($definition -notmatch '^Platform\(HostService\)://(.+)$') {
            throw ('Unsupported direct signal definition: ' + $definition)
        }
        $path = $Matches[1]
        try { $variable = $Description.Variables.Item($path) }
        catch { throw ('Required telemetry missing: ' + $path + '. Add the model output and rebuild before running this unit.') }
        if ($null -eq $variable -or -not $variable.IsReadable) {
            throw ('Required telemetry is not readable: ' + $path)
        }
    }
}

function Register-SweepDirectSignals($Mdm, $Groups) {
    $direct = @($Groups | Where-Object { Test-SweepDirectGroup $_ })
    $definitions = @($direct | ForEach-Object { $_.Signals } | Select-Object -Unique)
    if (-not $definitions.Count) { return }
    $missing = @($definitions | Where-Object { -not $Mdm.MeasurementConfiguration.Signals.Contains($_) })
    $inactive = @($definitions | Where-Object {
        $Mdm.MeasurementConfiguration.Signals.Contains($_) -and
        -not $Mdm.MeasurementConfiguration.Signals.Item($_).Active
    })
    # Never stop/restart a measurement the operator started in order to edit it.
    if (($missing.Count -or $inactive.Count) -and $Mdm.IsMeasuring) {
        throw 'Stop the existing ControlDesk measurement first; the new telemetry signals must be added before measurement starts.'
    }
    foreach ($definition in $missing) {
        [void]$Mdm.MeasurementConfiguration.Signals.Add([string]$definition)
    }
    foreach ($definition in $definitions) {
        $signal = $Mdm.MeasurementConfiguration.Signals.Item($definition)
        if (-not $signal.Active) { $signal.Active = $true }
        if (-not $signal.Active) { throw ('Cannot activate direct telemetry: ' + $definition) }
    }
}
