# One-shot combined stress: API hammer + browser VUs.
param(
  [string]$ApiUrl = "http://127.0.0.1:8000",
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [int]$ApiConc = 100,
  [string]$ApiDuration = "120s",
  [int]$PwWorkers = 4,
  [int]$RouteRepeats = 5
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "== API hammer (Go/fasthttp) ==" -ForegroundColor Cyan
Push-Location (Join-Path $root ".")
go run ./cmd/stress -base-url $ApiUrl -c $ApiConc -duration $ApiDuration -ramp 10s -out api.json
Pop-Location

Write-Host "== Browser VUs (Playwright) ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "playwright")
if (-not (Test-Path "node_modules")) { npm install }
$env:BASE_URL = $BaseUrl; $env:API_URL = $ApiUrl
$env:PW_WORKERS = "$PwWorkers"; $env:ROUTE_REPEATS = "$RouteRepeats"
npm run test:load
Pop-Location
