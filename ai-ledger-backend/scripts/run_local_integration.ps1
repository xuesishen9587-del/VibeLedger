[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 55432,
    [switch]$StopAfter,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunnerArguments
)

$ErrorActionPreference = "Stop"
$backendRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $backendRoot "docker-compose.integration.yml"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required for the local PostgreSQL 17 integration environment."
}

$env:VIBELEDGER_TEST_DB_PORT = [string]$Port
& docker compose -f $composeFile up -d --wait
if ($LASTEXITCODE -ne 0) {
    throw "Failed to start the local PostgreSQL 17 integration service."
}

$testExitCode = 1
try {
    $env:ENVIRONMENT = "test"
    $env:DATABASE_URL = "postgresql://vibeledger_test:vibeledger_test@127.0.0.1:$Port/vibeledger_test"
    $env:DB_SCHEMA = "vibeledger_test_runner"

    Push-Location $backendRoot
    try {
        & $Python scripts/run_integration_tests.py @RunnerArguments
        $testExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($StopAfter) {
        & docker compose -f $composeFile down
    }
}

exit $testExitCode
