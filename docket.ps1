# Docket on Windows — the same commands as the Makefile, for PowerShell.
#
#   .\docket.ps1 install      create .venv and install the dependencies
#   .\docket.ps1 env          create .env from .env.example, with a random ingest token
#   .\docket.ps1 test         run the test suite (offline)
#   .\docket.ps1 doctor       check every configured integration and say what to fix
#   .\docket.ps1 serve        run Docket on http://localhost:8000
#   .\docket.ps1 serve 8099   ... or on another port
#   .\docket.ps1 demo         load the seeded cases into a running Docket
#   .\docket.ps1 reset        clear runs and tickets from a running Docket
#   .\docket.ps1 replay       run in replay mode
#   .\docket.ps1 fixtures     regenerate the test fixtures and demo seeds
#   .\docket.ps1 mcp          run the read-only MCP server on stdio
#   .\docket.ps1 clean        remove caches, dry-run output and the local database
#
# If PowerShell refuses to run this file ("running scripts is disabled"), allow it for
# this session only:   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

[CmdletBinding()]
param(
    [Parameter(Position = 0)] [string] $Command = "help",
    [Parameter(Position = 1)] [string] $Port = "8000"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

function Find-SystemPython {
    # `py -3` is the Windows launcher and is the most reliable; fall back to python.exe.
    # Returned as (exe, args[]) so a single-element case cannot produce a descending
    # range: in PowerShell 1..0 is @(1,0), which would pass $null as the first argument.
    if (Get-Command py -ErrorAction SilentlyContinue)     { return @{ Exe = "py";     Args = @("-3") } }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Exe = "python"; Args = @() } }
    throw "No Python found. Install Python 3.11 or newer from python.org and tick 'Add to PATH'."
}

function Assert-Venv {
    if (-not (Test-Path $VenvPython)) {
        throw "No virtual environment yet. Run:  .\docket.ps1 install"
    }
}

function Import-DotEnv {
    # Docket reads its settings from the process environment. This mirrors what
    # `set -a; . ./.env` does on macOS and Linux, including inline comments.
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) { return }
    foreach ($line in Get-Content $envFile) {
        $text = $line.Trim()
        if ($text.Length -eq 0 -or $text.StartsWith("#")) { continue }
        $split = $text.IndexOf("=")
        if ($split -lt 1) { continue }
        $name = $text.Substring(0, $split).Trim()
        $value = $text.Substring($split + 1)
        # strip an inline comment that follows whitespace, as bash does
        $value = [regex]::Replace($value, '\s+#.*$', '')
        Set-Item -Path "env:$name" -Value $value.Trim()
    }
}

function Invoke-Api([string] $Method, [string] $Path) {
    Import-DotEnv
    $headers = @{}
    if ($env:DOCKET_API_TOKEN) { $headers["Authorization"] = "Bearer $($env:DOCKET_API_TOKEN)" }
    try {
        $result = Invoke-RestMethod -Method $Method -Uri "http://localhost:$Port$Path" -Headers $headers -TimeoutSec 120
        $result | ConvertTo-Json -Depth 8
    } catch {
        Write-Host "Could not reach Docket on port $Port. Start it with:  .\docket.ps1 serve $Port" -ForegroundColor Yellow
        throw
    }
}

switch ($Command.ToLower()) {

    "install" {
        $py = Find-SystemPython
        $pyArgs = $py.Args
        $version = & $py.Exe @pyArgs -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
        Write-Host "Using Python $version"
        & $py.Exe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
        if ($LASTEXITCODE -ne 0) { throw "Docket needs Python 3.11 or newer; found $version" }

        # Deliberately NOT quiet. A first install downloads fastapi, pydantic, anthropic,
        # PyGithub and the rest: several minutes on a cold machine. Silence looks like a hang.
        Write-Host "[1/3] creating the virtual environment in .venv ..." -ForegroundColor Cyan
        & $py.Exe @pyArgs -m venv .venv
        if (-not (Test-Path $VenvPython)) { throw "venv creation failed: $VenvPython was not created" }

        Write-Host "[2/3] upgrading pip ..." -ForegroundColor Cyan
        & $VenvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "pip could not be upgraded" }

        Write-Host "[3/3] installing Docket and its dependencies (this can take a few minutes) ..." -ForegroundColor Cyan
        & $VenvPython -m pip install -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { throw "dependency install failed - scroll up for pip's error" }

        Write-Host ""
        Write-Host "installed. next:  .\docket.ps1 env   then  .\docket.ps1 test" -ForegroundColor Green
    }

    "env" {
        Assert-Venv
        if (Test-Path ".env") {
            Write-Host ".env already exists, leaving it alone"
        } else {
            $token = & $VenvPython -c "import secrets; print(secrets.token_urlsafe(24))"
            (Get-Content ".env.example") -replace '^DOCKET_INGEST_TOKEN=change-me', "DOCKET_INGEST_TOKEN=$token" |
                Set-Content ".env" -Encoding utf8
            Write-Host "wrote .env with a random DOCKET_INGEST_TOKEN" -ForegroundColor Green
            Write-Host "put the same token in the copy of agent-setup\docket-env.ps1 you install in the governed repo"
        }
    }

    "test"     { Assert-Venv; & $VenvPython -m pytest backend/tests -q }

    "fixtures" { Assert-Venv; & $VenvPython backend/tests/fixtures/build_fixtures.py }

    "doctor"   { Assert-Venv; Import-DotEnv; & $VenvPython -m docket.doctor }

    "doctor-offline" { Assert-Venv; Import-DotEnv; & $VenvPython -m docket.doctor --offline }

    "serve" {
        Assert-Venv; Import-DotEnv
        Write-Host "Docket on http://localhost:$Port  (Ctrl-C to stop)" -ForegroundColor Green
        & $VenvPython -m uvicorn "docket.server:get_app" --factory --port $Port
    }

    "replay" {
        Assert-Venv; Import-DotEnv
        $env:DOCKET_MODE = "replay"
        Write-Host "Docket on http://localhost:$Port in REPLAY mode" -ForegroundColor Green
        & $VenvPython -m uvicorn "docket.server:get_app" --factory --port $Port
    }

    "demo"  { Invoke-Api "POST" "/demo/seed" }
    "reset" { Invoke-Api "POST" "/demo/reset" }
    "check" { Invoke-Api "GET"  "/healthz" }

    "mcp"   { Assert-Venv; Import-DotEnv; & $VenvPython -m docket.mcp_server }

    "clean" {
        foreach ($p in @(".pytest_cache", "out", "docket.sqlite")) {
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
        Get-ChildItem -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "cleaned"
    }

    default {
        Write-Host ""
        Write-Host "Docket on Windows" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  .\docket.ps1 install       create .venv and install dependencies"
        Write-Host "  .\docket.ps1 env           create .env with a random ingest token"
        Write-Host "  .\docket.ps1 test          run the test suite, offline"
        Write-Host "  .\docket.ps1 doctor        check the setup and say what to fix"
        Write-Host "  .\docket.ps1 serve [port]  run Docket (default 8000)"
        Write-Host "  .\docket.ps1 demo          load the seeded cases"
        Write-Host "  .\docket.ps1 reset         clear runs and tickets"
        Write-Host "  .\docket.ps1 replay [port] run in replay mode"
        Write-Host "  .\docket.ps1 fixtures      regenerate fixtures and demo seeds"
        Write-Host "  .\docket.ps1 mcp           run the read-only MCP server"
        Write-Host "  .\docket.ps1 clean         remove caches and the local database"
        Write-Host ""
        Write-Host "First time:  .\docket.ps1 install ; .\docket.ps1 env ; .\docket.ps1 test ; .\docket.ps1 serve"
        Write-Host ""
    }
}
