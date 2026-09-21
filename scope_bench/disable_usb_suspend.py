"""Disable USB selective suspend for the oscilloscope and the hub it sits behind.

A flaky USBTMC link that drops individual queries is a classic selective-suspend
symptom. This is a persistent host-side change and needs an elevated shell.
"""
import subprocess
import sys

SCRIPT = r'''
$ErrorActionPreference = 'Continue'
$scope = Get-PnpDevice -Class USBTestAndMeasurementDevice -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $scope) { 'no USB TMC device found'; exit 1 }
$chain = @($scope.InstanceId)
# Walk up to the parent hub so the port the scope uses also stops suspending.
$current = $scope
for ($i = 0; $i -lt 4; $i++) {
    $parent = (Get-PnpDeviceProperty -InstanceId $current.InstanceId -KeyName 'DEVPKEY_Device_Parent' -ErrorAction SilentlyContinue).Data
    if (-not $parent) { break }
    $chain += $parent
    $current = Get-PnpDevice -InstanceId $parent -ErrorAction SilentlyContinue
    if (-not $current) { break }
}
foreach ($instance in $chain) {
    $key = "HKLM:\SYSTEM\CurrentControlSet\Enum\$instance\Device Parameters"
    if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
    New-ItemProperty -Path $key -Name 'SelectiveSuspendEnabled' -Value 0 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $key -Name 'AllowIdleIrpInD3' -Value 0 -PropertyType DWord -Force | Out-Null
    $check = (Get-ItemProperty -Path $key -Name 'SelectiveSuspendEnabled' -ErrorAction SilentlyContinue).SelectiveSuspendEnabled
    "disabled suspend: $instance (SelectiveSuspendEnabled=$check)"
}
'''


def main():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", SCRIPT],
        capture_output=True, text=True)
    print(result.stdout.strip())
    if result.stderr.strip():
        print("stderr:", result.stderr.strip()[:500], file=sys.stderr)


if __name__ == "__main__":
    main()
