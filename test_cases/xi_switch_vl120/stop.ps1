Set-Content -LiteralPath (Join-Path $PSScriptRoot 'stop_request.json') -Value '{}' -Encoding ASCII
Write-Output 'Stop requested; the controller will disable Enable1 and preserve completed data.'
