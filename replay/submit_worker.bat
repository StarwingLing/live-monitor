@echo off
rem replay local submit worker (double-click to run)
cd /d "%~dp0"
uv --version >nul 2>&1
if errorlevel 1 goto NOUV
uv run python deploy\submit_worker.py --base http://<服务器IP>/replay
goto END
:NOUV
echo [ERROR] uv not found. Install uv first, then retry.
:END
pause
