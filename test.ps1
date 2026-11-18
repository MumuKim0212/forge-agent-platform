<#
Runs both test suites: backend pytest and frontend vitest.

Usage:
  .\test.ps1            both suites
  .\test.ps1 backend    backend only
  .\test.ps1 frontend   frontend only

Docker-dependent backend tests skip cleanly when Docker is absent. Skips are
expected, not failures - the summary line below distinguishes them.
#>
param(
    [ValidateSet("all", "backend", "frontend")]
    [string]$Only = "all"
)

# Deliberately NOT setting $ErrorActionPreference = "Stop": in PowerShell 5.1
# that turns any stderr line from pip/npm into a terminating error. Same
# reasoning as dev.ps1.

$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

$backendCode = 0
$frontendCode = 0

if ($Only -eq "all" -or $Only -eq "backend") {
    Push-Location $backendDir
    try {
        if (-not (Test-Path $venvPython)) {
            Write-Host "[backend] no venv - run .\dev.ps1 once first, or:"
            Write-Host "          python -m venv backend\.venv"
            $backendCode = 1
        }
        else {
            # Reinstall is cheap when nothing changed and covers the case where
            # a dependency was added since this venv was last touched (tzdata,
            # added later, is the reason this line exists: without it
            # ZoneInfo cannot resolve even "UTC" on Windows and the app refuses
            # to start).
            #
            # the sync is against backend\constraints.txt, the exact
            # versions CI runs. It happens on EVERY run (the venv must already
            # exist, but it is not left as it was): an older venv is re-aligned
            # to the pins here, so a dev venv and CI cannot quietly differ once
            # this has run. Windows-only packages are not in the file and float.
            Write-Host "[backend] syncing dependencies (pinned by constraints.txt)..."
            & $venvPython -m pip install -c constraints.txt -e ".[dev]" -q
            # A failed install leaves the venv unaligned with constraints.txt;
            # stop here rather than carry on against whatever versions it has. Exit code is
            # checked explicitly because $ErrorActionPreference is deliberately not "Stop".
            if ($LASTEXITCODE -ne 0) {
                Write-Host "[backend] the constrained install failed (pip exit code $LASTEXITCODE): the venv is not aligned with constraints.txt - fix the pip error above and re-run."
                exit 1
            }

            Write-Host "[backend] pytest..."
            & $venvPython -m pytest -q
            $backendCode = $LASTEXITCODE
        }
    }
    finally {
        Pop-Location
    }
}

if ($Only -eq "all" -or $Only -eq "frontend") {
    Push-Location $frontendDir
    try {
        if (-not (Test-Path "node_modules")) {
            Write-Host "[frontend] installing dependencies..."
            cmd /c "npm install"
        }
        # cmd /c rather than bare npm: PowerShell resolves `npm` to its .ps1
        # wrapper, which has exited silently in some setups.
        Write-Host "[frontend] tsc -b..."
        cmd /c "npx tsc -b"
        $tscCode = $LASTEXITCODE

        Write-Host "[frontend] vitest..."
        cmd /c "npm test"
        $frontendCode = $LASTEXITCODE
        if ($tscCode -ne 0) { $frontendCode = $tscCode }
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
if ($Only -eq "all" -or $Only -eq "backend") {
    if ($backendCode -eq 0) { Write-Host "backend:  PASS" } else { Write-Host "backend:  FAIL (exit $backendCode)" }
}
if ($Only -eq "all" -or $Only -eq "frontend") {
    if ($frontendCode -eq 0) { Write-Host "frontend: PASS" } else { Write-Host "frontend: FAIL (exit $frontendCode)" }
}

if ($backendCode -ne 0 -or $frontendCode -ne 0) { exit 1 }
exit 0
