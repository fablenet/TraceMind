# TraceMind test runner for Windows (PowerShell)
# Usage: .\scripts\run_tests.ps1 [pytest args...]
# Example: .\scripts\run_tests.ps1 tests/unit -q

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $scriptDir
$venvDir = Join-Path $rootDir "venv"
$python = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "venv not found at '$venvDir'. Create it first: python -m venv venv"
    exit 1
}

Push-Location $rootDir
try {
    & $python -m pytest @args
} finally {
    Pop-Location
}
exit $LASTEXITCODE
exit $LASTEXITCODE
