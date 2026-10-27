param(
    [Parameter(Mandatory=$true)][string]$BatchPath,
    [string]$OutputDirectory = ""
)

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $root 'scope_bench\.venv\Scripts\python.exe'
$tools = Join-Path $root 'analysis_tools\pacc_ripple'
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $BatchPath 'showcase_analysis'
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$metrics = Join-Path $OutputDirectory 'showcase_metrics.csv'
$figure = Join-Path $OutputDirectory 'showcase_angle_comparison'

& $python (Join-Path $tools 'collect_scope_batch_metrics.py') $BatchPath --channel CH1 --output $metrics
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $tools 'analyze_showcase_batch.py') $metrics $OutputDirectory
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $tools 'plot_three_angle_comparison.py') (Join-Path $OutputDirectory 'showcase_triplets.csv') $figure `
    --order 'SC0500,SC0505,SC0510,SC0515,SC0520' `
    --title 'VL=120 V: broadband AC RMS first, carrier groups diagnostic'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $python (Join-Path $tools 'plot_showcase_tradeoff.py') $metrics (Join-Path $OutputDirectory 'showcase_tradeoff')
exit $LASTEXITCODE
