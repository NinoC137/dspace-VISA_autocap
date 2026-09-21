# Best-effort host-side recovery for a wedged MXO38 USB link.
# Prints one JSON object on stdout. A physical power cycle of the oscilloscope is
# still required when the instrument's own USB-TMC stack has stopped answering.
$ErrorActionPreference = 'Stop'
try {
    $device = Get-PnpDevice -Class USBTestAndMeasurementDevice -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $device) {
        [pscustomobject]@{ok=$false; error='No USB TMC device is enumerated.'} | ConvertTo-Json -Compress
        exit 0
    }
    Disable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
    Start-Sleep -Seconds 3
    Enable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
    Start-Sleep -Seconds 5
    $status = (Get-PnpDevice -InstanceId $device.InstanceId).Status
    [pscustomobject]@{ok=($status -eq 'OK'); instance_id=$device.InstanceId; status=$status;
        note='Re-enumeration cannot clear a wedged instrument; power-cycle the scope if it still times out.'} |
        ConvertTo-Json -Compress
} catch {
    [pscustomobject]@{ok=$false; error=$_.Exception.Message} | ConvertTo-Json -Compress
    exit 0
}
