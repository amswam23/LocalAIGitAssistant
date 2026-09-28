<#
.SYNOPSIS
    Zero-install launcher for Git Local Assistant.

.DESCRIPTION
    Creates a local virtual environment on first run (if one doesn't already
    exist), installs the project into it, and starts the application.
    You do not need to activate the virtual environment yourself.

.EXAMPLE
    .\run.ps1

.EXAMPLE
    .\run.ps1 --port 8080 --repo C:\Users\me\projects\my-app
#>

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs
)

$ErrorActionPreference = "Stop"

function Find-Python {
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) { return $candidate }
    }
    return $null
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PythonLauncher = Find-Python
if (-not $PythonLauncher) {
    Write-Error "Python 3.10+ was not found on PATH. Install it from https://www.python.org/downloads/ and try again."
    exit 1
}

$VenvDir = Join-Path $ScriptDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Setting up a local virtual environment (first run only)..." -ForegroundColor Cyan
    & $PythonLauncher -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to create the virtual environment."
        exit 1
    }
}

Write-Host "Installing / updating Git Local Assistant..." -ForegroundColor Cyan
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -e "$ScriptDir"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to install the application."
    exit 1
}

Write-Host "Starting Git Local Assistant..." -ForegroundColor Green
& $VenvPython -m git_local_assistant @AppArgs
