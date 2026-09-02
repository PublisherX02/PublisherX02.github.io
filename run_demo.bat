@echo off
setlocal
pushd "%~dp0"

start "Trading Agent Dashboard" cmd /k "python dashboard\app.py"
python -m run_agent %*
set "RUN_AGENT_EXIT=%ERRORLEVEL%"

popd
exit /b %RUN_AGENT_EXIT%
