@echo off
setlocal
cd /d "%~dp0"

rem Forge local menu. Thin wrapper around dev.ps1, test.ps1 and the commands in README.
rem Nothing here changes how those scripts behave; it only picks which one to run.
rem
rem ASCII only, on purpose: cmd mis-parses non-ASCII lines in a batch file when the
rem console code page differs from the file encoding (a Korean label got split and its
rem tail ran as a command). No chcp, no Korean text in this file.

set "VENV_PY=%~dp0backend\.venv\Scripts\python.exe"

rem The server, the worker and alembic refuse to start without FORGE_ENV. dev.ps1 supplies
rem "dev" only when neither the environment nor backend\.env sets it; do the same for the
rem worker-only option here, and never override a value somebody set on purpose.
if not defined FORGE_ENV (
  findstr /r /c:"^[ ]*FORGE_ENV[ ]*=[ ]*[^ ]" "%~dp0backend\.env" >nul 2>&1
  if errorlevel 1 set "FORGE_ENV=dev"
)

:menu
cls
echo.
echo  ==============================
echo    Forge local menu
echo  ==============================
echo.
echo    1  Dev servers + worker (dev.ps1)
echo    2  Dev servers only, no worker (dev.ps1 -NoWorker)
echo    3  Worker only: processes builds
echo.
echo    4  Tests: all
echo    5  Tests: backend only
echo    6  Tests: frontend only
echo.
echo    7  Load demo data (for AUTH_MODE=dev)
echo    8  Build sandbox / proxy Docker images
echo.
echo    0  Exit
echo.
set "choice="
set /p "choice=Enter a number: "

if "%choice%"=="1" goto dev
if "%choice%"=="2" goto dev_noworker
if "%choice%"=="3" goto worker
if "%choice%"=="4" goto test_all
if "%choice%"=="5" goto test_backend
if "%choice%"=="6" goto test_frontend
if "%choice%"=="7" goto demo
if "%choice%"=="8" goto images
if "%choice%"=="0" goto end
echo.
echo Not a menu number: %choice%
pause
goto menu

:dev
echo.
echo Starting the dev servers and the worker. Press Ctrl+C to stop.
powershell -NoProfile -File "%~dp0dev.ps1"
goto done

:dev_noworker
echo.
echo Starting the dev servers without the worker. Press Ctrl+C to stop.
powershell -NoProfile -File "%~dp0dev.ps1" -NoWorker
goto done

:worker
call :need_venv || goto done
echo.
echo Starting the worker. Press Ctrl+C to stop.
pushd "%~dp0backend"
"%VENV_PY%" -m app.worker
popd
goto done

:test_all
powershell -NoProfile -File "%~dp0test.ps1"
goto done

:test_backend
powershell -NoProfile -File "%~dp0test.ps1" backend
goto done

:test_frontend
powershell -NoProfile -File "%~dp0test.ps1" frontend
goto done

:demo
call :need_venv || goto done
echo.
pushd "%~dp0backend"
"%VENV_PY%" -m app.demo_data
popd
goto done

:images
echo.
echo Building the Docker images. Docker Desktop must be running.
pushd "%~dp0backend"
docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .
if errorlevel 1 goto images_failed
docker build -t forge-agent-sandbox:latest docker/agent-sandbox
if errorlevel 1 goto images_failed
docker build -t forge-agent-sandbox-openai:latest -f docker/agent-sandbox-openai/Dockerfile .
if errorlevel 1 goto images_failed
popd
goto done

:images_failed
popd
echo.
echo An image build failed. Read the output above.
goto done

:done
echo.
pause
goto menu

:need_venv
if exist "%VENV_PY%" exit /b 0
echo.
echo backend\.venv does not exist. Run option 1 once first to create it.
exit /b 1

:end
endlocal
