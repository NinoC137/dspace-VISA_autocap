$ErrorActionPreference = "Continue"
cd F:\ZYS_HYBASE\automation\scope_bench
$py = ".venv\Scripts\python.exe"
$root = "F:\ZYS_HYBASE\automation\scope_results"

Write-Output "=== 1) scope_api capture (creates the capture dir) ==="
$out = & $py -u scope_api.py --config bench_config.json capture --directory $root --label tek_detached --channels CH2 --analysis-channel CH2
$out | Out-String | Write-Output
$dir = ($out | ConvertFrom-Json).directory
Write-Output "capture dir = $dir"

Write-Output ""
Write-Output "=== 2) scope_api capture-start (detached worker) ==="
$out2 = & $py -u scope_api.py --config bench_config.json capture-start --directory $dir --workdir "$root\_sessions\manual_detached"
$out2 | Out-String | Write-Output
$pid2 = ($out2 | ConvertFrom-Json).pid

Start-Sleep -Seconds 20
Write-Output ""
Write-Output "=== 3) after 20 s ==="
$alive = Get-Process -Id $pid2 -ErrorAction SilentlyContinue
Write-Output ("worker pid {0} alive: {1}" -f $pid2, [bool]$alive)
Write-Output "--- worker.log ---"
Get-Content "$dir\worker.log" -ErrorAction SilentlyContinue | Select-Object -Last 20
Write-Output "--- status.json ---"
Get-Content "$dir\status.json" -ErrorAction SilentlyContinue
Write-Output "--- error.txt ---"
Get-Content "$dir\error.txt" -ErrorAction SilentlyContinue | Select-Object -Last 20
