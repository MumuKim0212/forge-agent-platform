<#
One-shot local dev runner for the Forge platform skeleton.
Sets up the backend (venv, deps, migrations, seed) and frontend (npm deps),
then runs both servers and the build worker. Ctrl+C stops the frontend and tears
down the backend and the worker.

  .\dev.ps1             backend :8000 + frontend :5173 + build worker
  .\dev.ps1 -NoWorker   without the worker (screens only, or run it yourself:
                        cd backend; .venv\Scripts\python -m app.worker)
#>
param(
    [switch]$NoWorker
)

# Note: deliberately NOT setting $ErrorActionPreference = "Stop" here.
# With that set, PowerShell 5.1 treats ANY stderr line from a native command
# (npm, pip, etc. printing a harmless notice) as a terminating error, which
# was killing this script the instant `npm run dev` wrote anything to stderr.
$root = $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$venvPython = Join-Path $backendDir ".venv\Scripts\python.exe"

# --- Backend setup ---
Push-Location $backendDir
try {
    if (-not (Test-Path ".venv")) {
        Write-Host "[backend] creating venv..."
        python -m venv .venv
    }

    # install against backend\constraints.txt (the exact versions CI
    # runs). This line runs on EVERY start, not only when the venv is created:
    # an existing venv is re-aligned to the pins here, so one that resolved
    # newer or older versions months ago is up/downgraded to match CI on its
    # next run (verified: pip replaces an installed version that violates a
    # constraint). Windows-only packages are not in the file and float.
    Write-Host "[backend] installing dependencies (pinned by constraints.txt)..."
    & $venvPython -m pip install -c constraints.txt -e ".[dev]" -q
    # A failed install leaves the venv unaligned with constraints.txt;
    # stop here rather than carry on against whatever versions it has. Exit code is
    # checked explicitly because $ErrorActionPreference is deliberately not "Stop".
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[backend] the constrained install failed (pip exit code $LASTEXITCODE): the venv is not aligned with constraints.txt - fix the pip error above and re-run."
        exit 1
    }

    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
    }

    # FORGE_ENV has no default any more -- the
    # server, the worker and alembic all refuse to start without it. This script is the
    # dev launcher, so it supplies "dev" for the processes it starts, but ONLY when
    # neither the environment nor backend\.env says anything (a blank line counts as
    # nothing): it must never override a value somebody set on purpose, and a
    # FORGE_ENV=prod in .env stays prod. It never edits .env itself.
    if (-not $env:FORGE_ENV) {
        $modeInEnvFile = (Test-Path ".env") -and (Select-String -Path ".env" -Pattern '^\s*FORGE_ENV\s*=\s*\S' -Quiet)
        if (-not $modeInEnvFile) {
            $env:FORGE_ENV = "dev"
            Write-Host "[backend] FORGE_ENV is not set (environment or backend\.env): using FORGE_ENV=dev for the processes this script starts. Add FORGE_ENV=dev to backend\.env to make it permanent."
        }
    }

    # --- ADR 0023 §Amendment 8: dev brings up its own shared
    # service-Postgres, so no human runs `docker run` on this box (the
    # operator's reasons for doing it by hand -- durability, tuning, an
    # upgrade window -- don't apply to a throwaway dev instance). Container
    # name / image / max_connections come from Settings, never repeated as
    # PowerShell literals -- one venv
    # Python call reads them back.
    Write-Host "[backend] checking dev service-Postgres..."
    $pgSettingsRaw = & $venvPython -c "from app.config import settings; print(settings.service_postgres_container_name); print(settings.service_postgres_image); print(settings.service_postgres_max_connections)"
    $pgContainerName = $pgSettingsRaw[0]
    $pgImage = $pgSettingsRaw[1]
    $pgMaxConnections = $pgSettingsRaw[2]

    # ADR 0023 §Amendment 11: the instance's data lives in a NAMED
    # volume so `docker rm` of the container does not discard it. The name is
    # derived from the container-name setting (so a renamed container gets its
    # own volume and never adopts another instance's data) -- no extra Settings
    # field. The mount target is the image's own declared VOLUME, read back from
    # the image rather than written as a literal: postgres:18 declares
    # /var/lib/postgresql (PGDATA is /var/lib/postgresql/18/docker under it),
    # older majors declare .../data, and a mount at the wrong path leaves the
    # data in an anonymous volume again with nothing erroring.
    $pgVolume = "$pgContainerName-data"
    $pgVolumesRaw = docker image inspect $pgImage --format '{{json .Config.Volumes}}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[backend] pulling $pgImage..."
        docker pull $pgImage | Out-Null
        $pgVolumesRaw = docker image inspect $pgImage --format '{{json .Config.Volumes}}' 2>$null
    }
    $pgVolumesExit = $LASTEXITCODE
    $pgDataDirMatch = [regex]::Match("$pgVolumesRaw", '"(/[^"]+)"')
    if ($pgVolumesExit -ne 0 -or -not $pgDataDirMatch.Success) {
        Write-Host "Could not read the data directory (the declared VOLUME) of image '$pgImage' - check 'docker image inspect $pgImage'."
        exit 1
    }
    $pgDataDir = $pgDataDirMatch.Groups[1].Value

    # ADR 0023 §Amendment 10: SERVICE_POSTGRES_ADMIN_DSN is the SINGLE
    # source of truth for host/port/role/password -- there is no second
    # setting holding the port. If one is already in .env, its port is
    # parsed out to publish on (an operator/dev may have customized it);
    # if absent, the port is 55432 (not 5432, so a Postgres already
    # installed on this machine never answers instead) and this is the
    # ONLY place in the repo that spells that literal.
    $envFile = Join-Path $backendDir ".env"
    $envText = Get-Content $envFile -Raw
    $dsnMatch = [regex]::Match($envText, '(?m)^\s*SERVICE_POSTGRES_ADMIN_DSN\s*=\s*(\S+)\s*$')
    $pgDsnAlreadySet = $dsnMatch.Success -and ($dsnMatch.Groups[1].Value.Trim() -ne "")
    $pgPort = 55432
    $pgAdminPassword = $null
    if ($pgDsnAlreadySet) {
        $existingDsn = $dsnMatch.Groups[1].Value.Trim()
        $portMatch = [regex]::Match($existingDsn, ":(\d+)/[^/]*$")
        if ($portMatch.Success) { $pgPort = [int]$portMatch.Groups[1].Value }
        $pwMatch = [regex]::Match($existingDsn, "://[^:]+:([^@]+)@")
        if ($pwMatch.Success) { $pgAdminPassword = $pwMatch.Groups[1].Value }
    }

    $pgInspect = docker inspect $pgContainerName --format "{{.State.Running}}" 2>$null
    $pgContainerExists = ($LASTEXITCODE -eq 0)
    $pgJustCreated = $false
    $pgReusedVolume = $false
    docker volume inspect $pgVolume 2>$null | Out-Null
    $pgVolumeExists = ($LASTEXITCODE -eq 0)

    if ($pgContainerExists -and (-not $pgDsnAlreadySet)) {
        # A container that already exists but no DSN
        # in backend\.env means this script cannot know forge_admin's
        # password -- it is only ever generated in the fresh-container
        # branch below. Writing a DSN with a blank password here would
        # look complete and silently be useless. Fail loudly instead and
        # name the operator action, per the review that caught this.
        Write-Host "Dev service-Postgres container '$pgContainerName' already exists, but SERVICE_POSTGRES_ADMIN_DSN is not set in backend\.env."
        Write-Host "This script cannot recover the forge_admin password for an already-existing container."
        Write-Host "Either set SERVICE_POSTGRES_ADMIN_DSN in backend\.env to the existing container's forge_admin credential,"
        Write-Host "or discard it: docker rm -f $pgContainerName ; docker volume rm $pgVolume  (the volume holds its data - this DELETES every service database)."
        exit 1
    }

    if ((-not $pgContainerExists) -and $pgVolumeExists) {
        # ADR 0023 §Amendment 11: the volume outlived its container and holds the
        # old forge_admin role and password. Never invent a new password against
        # that data; reuse the DSN's, or stop and say how to choose.
        if (-not $pgDsnAlreadySet) {
            Write-Host "Volume '$pgVolume' holds an earlier dev service-Postgres, but SERVICE_POSTGRES_ADMIN_DSN is not set in backend\.env, so its forge_admin password is unknown."
            Write-Host "Either restore SERVICE_POSTGRES_ADMIN_DSN in backend\.env, or discard the data: docker volume rm $pgVolume  (this DELETES every service database in it)."
            exit 1
        }
        if (-not $pgAdminPassword) {
            Write-Host "Volume '$pgVolume' holds an earlier dev service-Postgres, but the SERVICE_POSTGRES_ADMIN_DSN in backend\.env carries no password to reuse against it."
            Write-Host "Either put the forge_admin password back into that DSN, or discard the data: docker volume rm $pgVolume  (this DELETES every service database in it)."
            exit 1
        }
        $pgReusedVolume = $true
    }

    if ($pgContainerExists) {
        # a container made before the named volume existed keeps its data in
        # an anonymous volume. It still starts (this script never removes or
        # recreates a container that exists); the warning, printed on every run
        # until it is migrated, names the one-time migration.
        $pgMounts = docker inspect $pgContainerName --format '{{range .Mounts}}{{.Type}}|{{.Name}}|{{.Destination}};{{end}}' 2>$null
        $pgAnonVolume = $null
        foreach ($m in ("$pgMounts" -split ";")) {
            $f = $m -split "\|"
            if ($f.Count -eq 3 -and $f[0] -eq "volume" -and $f[2] -eq $pgDataDir -and $f[1] -match '^[0-9a-f]{64}$') { $pgAnonVolume = $f[1] }
        }
        if ($pgAnonVolume) {
            Write-Host "[backend] WARNING: '$pgContainerName' keeps its data in an anonymous volume ($pgAnonVolume); 'docker rm' of the container would discard every service database."
            Write-Host "          Migrate once, then re-run this script (it recreates the container on the named volume '$pgVolume', reusing the DSN in backend\.env; the old anonymous volume is kept by 'docker rm', so a failed step loses nothing):"
            Write-Host "          docker stop $pgContainerName ; docker volume create $pgVolume ; docker run --rm -v ${pgAnonVolume}:/from -v ${pgVolume}:/to $pgImage cp -a /from/. /to/ ; docker rm $pgContainerName"
        }
    }

    if ($pgContainerExists -and $pgInspect.Trim() -eq "true") {
        Write-Host "[backend] dev service-Postgres already running -- leaving it alone."
    } elseif ($pgContainerExists) {
        Write-Host "[backend] starting existing dev service-Postgres container..."
        docker start $pgContainerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Failed to start the existing dev service-Postgres container ($pgContainerName)."
            exit 1
        }
    } else {
        if (-not $pgAdminPassword) {
            $pgAdminPassword = [System.Guid]::NewGuid().ToString("N")
        }
        $pgBootstrapPassword = [System.Guid]::NewGuid().ToString("N")
        Write-Host "[backend] creating dev service-Postgres container ($pgImage) on 127.0.0.1:$pgPort, data in volume $pgVolume..."
        docker run -d --name $pgContainerName `
            -e "POSTGRES_PASSWORD=$pgBootstrapPassword" `
            -p "127.0.0.1:${pgPort}:5432" `
            -v "${pgVolume}:${pgDataDir}" `
            $pgImage `
            postgres -c "max_connections=$pgMaxConnections" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Failed to create the dev service-Postgres container ($pgContainerName)."
            exit 1
        }
        # A reused volume already carries the role (checked below); only a fresh
        # data directory needs it created.
        $pgJustCreated = (-not $pgReusedVolume)
    }

    # Readiness: `pg_isready -h 127.0.0.1` inside the container, NOT a bare
    # TCP connect and NOT plain `pg_isready` either -- the official image
    # runs a temporary, Unix-socket-only server for init scripts before the
    # real one (which listens on TCP) replaces it, so the published port
    # accepts a connection, and socket-default `pg_isready`, both read
    # ready a moment before Postgres genuinely can answer (verified against
    # this exact image). Bounded attempts, clear failure
    # message, exit 1 -- the same shape as the backend-readiness wait
    # further down in this script.
    Write-Host "[backend] waiting for dev service-Postgres to accept connections..."
    $pgReady = $false
    for ($i = 0; $i -lt 60; $i++) {
        docker exec $pgContainerName pg_isready -h 127.0.0.1 -U postgres | Out-Null
        if ($LASTEXITCODE -eq 0) { $pgReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $pgReady) {
        Write-Host "Dev service-Postgres never became ready - check 'docker logs $pgContainerName'"
        exit 1
    }

    if ($pgReusedVolume) {
        # The data predates this container: forge_admin normally exists already, and
        # POSTGRES_PASSWORD above was ignored (the image only applies it to an empty
        # data directory). Create the role only if it is missing (a volume whose first
        # run never got that far), with the DSN's password -- never a new one.
        $pgRole = docker exec $pgContainerName psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname = 'forge_admin'"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Could not query roles on the reused dev service-Postgres volume '$pgVolume' - check 'docker logs $pgContainerName'"
            exit 1
        }
        if ("$pgRole".Trim() -ne "1") {
            $pgJustCreated = $true
        }
    }

    if ($pgJustCreated) {
        # ADR 0023 §Amendment 5: the platform's admin role is CREATEDB
        # CREATEROLE, never SUPERUSER -- even in dev. The image's own
        # bootstrap user ("postgres", POSTGRES_PASSWORD above) IS a
        # superuser by construction (unavoidable for a fresh Postgres
        # image); a separate, deliberately weaker role is what
        # SERVICE_POSTGRES_ADMIN_DSN actually points at.
        docker exec $pgContainerName psql -U postgres -c "CREATE ROLE forge_admin WITH LOGIN PASSWORD '$pgAdminPassword' CREATEDB CREATEROLE NOSUPERUSER" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Failed to create the forge_admin role on the dev service-Postgres."
            exit 1
        }
    }

    if ($pgReusedVolume) {
        # Prove the DSN in .env opens the reused data, from the host through the
        # published port -- exactly the path the platform takes. (A check inside the
        # container proves nothing: the image trusts 127.0.0.1 there.)
        & $venvPython -c "import sys, psycopg; psycopg.connect(sys.argv[1], connect_timeout=5).close()" $existingDsn 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "The SERVICE_POSTGRES_ADMIN_DSN in backend\.env does not open the data in volume '$pgVolume' (wrong password or port)."
            Write-Host "Either restore the DSN that volume was created with, or discard the data: docker volume rm $pgVolume  (this DELETES every service database)."
            # The container made in this run is removed so the next run re-checks the DSN instead of trusting it; the volume is untouched.
            docker rm -f $pgContainerName | Out-Null
            exit 1
        }
        Write-Host "[backend] reused volume $pgVolume; the DSN in backend\.env connects."
    }

    if (-not $pgDsnAlreadySet) {
        $pgDsn = "postgresql://forge_admin:${pgAdminPassword}@127.0.0.1:${pgPort}/postgres"
        Add-Content -Path $envFile -Value "SERVICE_POSTGRES_ADMIN_DSN=$pgDsn"
        Write-Host "[backend] wrote a generated SERVICE_POSTGRES_ADMIN_DSN to backend\.env"
    }

    # migrations are the ONLY thing that creates or alters the
    # platform schema, and the server refuses a DB that is not at the head. So a
    # failed `alembic upgrade head` must stop the script here -- it used to scroll
    # past and start a server that then died (or ran against a stale schema).
    # This script never deletes or modifies a database file itself.
    Write-Host "[backend] applying migrations..."
    & $venvPython -m alembic upgrade head
    $alembicExit = $LASTEXITCODE
    if ($alembicExit -ne 0) {
        Write-Host ""
        Write-Host "[backend] 'alembic upgrade head' FAILED (exit code $alembicExit) - not starting the server."
        # Read-only probe (sqlite3 mode=ro, nothing is written): does the dev DB
        # have platform tables but no alembic revision recorded?
        $devDb = "forge_dev.db"
        $dbState = ""
        if (Test-Path $devDb) {
            $probe = @'
import sqlite3,sys; c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True); t={r[0] for r in c.execute('select name from sqlite_master where type=?',('table',))}; n=len(t-{'alembic_version'}); v=c.execute('select count(*) from alembic_version').fetchone()[0] if 'alembic_version' in t else 0; print('UNSTAMPED' if n>0 and v==0 else 'OTHER')
'@
            $dbState = (& $venvPython -c $probe $devDb) -join ""
        }
        if ($dbState.Trim() -eq "UNSTAMPED") {
            Write-Host "backend\$devDb has tables but no Alembic revision recorded: it was created outside migrations"
            Write-Host "(by an older server's create_all, or by hand), so alembic cannot upgrade it - 'table already exists'."
            Write-Host "To fix it: if you need its data, copy the file somewhere first; then delete backend\$devDb"
            Write-Host "(and backend\$devDb-wal / -shm if present) YOURSELF - this script never deletes it - re-run dev.ps1,"
            Write-Host "and repopulate with: python -m app.demo_data"
        } else {
            Write-Host "Read the alembic output above, fix the cause, and re-run dev.ps1."
        }
        exit 1
    }

    Write-Host "[backend] starting server on :8000..."
    $backendProc = Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "8000" `
        -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput "uvicorn.log" -RedirectStandardError "uvicorn.err.log"

    # The build worker, started the same way and only AFTER the migration
    # above (the worker refuses a DB that is not at the Alembic head).
    # Without one every new request stays QUEUED. It inherits the FORGE_ENV resolved
    # earlier in this script. -NoWorker skips it.
    $workerProc = $null
    if (-not $NoWorker) {
        Write-Host "[worker] starting build worker..."
        $workerProc = Start-Process -FilePath $venvPython `
            -ArgumentList "-m", "app.worker" `
            -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput "worker.log" -RedirectStandardError "worker.err.log"
        # Touching .Handle caches it, so HasExited/ExitCode are reliable later
        # (PowerShell 5.1 can return $null for a redirected -PassThru process otherwise).
        $null = $workerProc.Handle
    }
}
finally {
    Pop-Location
}

# Wait for the backend to actually answer before starting the frontend.
# Uses a raw TCP connect check instead of Invoke-RestMethod/Invoke-WebRequest:
# those can hang or time out on "localhost" in some environments (IPv6 ::1
# resolution, proxy env vars) even when the server is already listening.
Write-Host "[backend] waiting for port 8000..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", 8000)
        if ($client.Connected) { $healthy = $true; break }
    } catch {}
    finally {
        $client.Close()
    }
    Start-Sleep -Seconds 1
}
if (-not $healthy) {
    Write-Host "Backend failed to start - check backend\uvicorn.err.log"
    Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
    if ($workerProc) { Stop-Process -Id $workerProc.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}
Write-Host "[backend] ready at http://localhost:8000"

# A short, bounded look at the worker (at most ~5 s, on top of the time the backend
# wait above already gave it). A worker that refuses to start (a DB not at head, a held
# worker lease because another worker is already running, a bad setting) exits within
# seconds; say so and carry on -- the frontend and backend still run. Not a supervisor:
# a worker that dies later is not noticed.
$workerNote = "skipped (-NoWorker): new requests stay QUEUED until a worker runs (cd backend; .venv\Scripts\python -m app.worker)"
if ($workerProc) {
    for ($i = 0; $i -lt 5 -and -not $workerProc.HasExited; $i++) { Start-Sleep -Seconds 1 }
    if ($workerProc.HasExited) {
        $workerNote = "EXITED at start (exit code $($workerProc.ExitCode)) - new requests stay QUEUED. Read backend\worker.err.log"
        Write-Host "[worker] the worker exited right after starting (exit code $($workerProc.ExitCode)). Last log lines (backend\worker.err.log):"
        if (Test-Path (Join-Path $backendDir "worker.err.log")) {
            Get-Content (Join-Path $backendDir "worker.err.log") -Tail 3 | ForEach-Object { Write-Host "          $_" }
        }
        Write-Host "[worker] carrying on without it: the backend and frontend still run."
    } else {
        $workerNote = "running (pid $($workerProc.Id)); logs backend\worker.log, backend\worker.err.log"
        Write-Host "[worker] running (pid $($workerProc.Id))"
    }
}

# --- What to do once it's up ---
# Printed here rather than left in the README because the way you
# get in: there is no longer an implicit identity, and the admin console is
# invisible unless you pick the seeded admin.
$envFile = Join-Path $backendDir ".env"
$authMode = "dev"
if (Test-Path $envFile) {
    $authLine = Select-String -Path $envFile -Pattern '^\s*AUTH_MODE\s*=\s*(\S+)' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($authLine) { $authMode = $authLine.Matches[0].Groups[1].Value }
}

Write-Host ""
Write-Host "  Open http://localhost:5173"
Write-Host "  AUTH_MODE=$authMode (from backend\.env; unset means dev)"
Write-Host "  Build worker: $workerNote"
if ($authMode -eq "dev") {
    Write-Host ""
    Write-Host "  Pick an identity at /login - the three seeded users differ by role:"
    Write-Host "    kim minji    (requester)  basic screens only"
    Write-Host "    lee dohyun   (approver)   + can decide in the approval console"
    Write-Host "    park seoyeon (admin)      + /admin console (5 of its 6 tabs -- 가입 승인 needs AUTH_MODE=local)"
    Write-Host ""
    Write-Host "  The admin console has no nav entry for the other two, and refuses"
    Write-Host "  direct navigation - sign in as park seoyeon to see it."
} else {
    Write-Host "  Real SSO is on: /login redirects to the configured IdP."
    Write-Host "  First admin comes from BOOTSTRAP_ADMIN_EMAILS (first login only)."
}
Write-Host ""
Write-Host "  Tests: .\test.ps1        (backend pytest + frontend vitest)"
Write-Host ""

# --- Frontend setup + run (foreground) ---
# Invoked via `cmd /c` rather than calling `npm`/`npm.ps1` directly: PowerShell
# resolves bare `npm` to npm's own .ps1 wrapper, which has caused this script
# to exit immediately with no output in some setups. cmd /c npm goes straight
# to npm.cmd and behaves like running it in a normal terminal.
Push-Location $frontendDir
try {
    if (-not (Test-Path "node_modules")) {
        Write-Host "[frontend] installing dependencies..."
        cmd /c "npm install"
        Write-Host "[frontend] npm install exited with code $LASTEXITCODE"
    }
    Write-Host "[frontend] starting dev server on :5173 (Ctrl+C to stop everything)..."
    cmd /c "npm run dev"
    Write-Host "[frontend] npm run dev exited with code $LASTEXITCODE"
}
finally {
    Pop-Location
    Write-Host "Stopping backend..."
    Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
    if ($workerProc) {
        Write-Host "Stopping worker..."
        Stop-Process -Id $workerProc.Id -Force -ErrorAction SilentlyContinue
    }
}
