@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
if errorlevel 1 goto failed
goto end
:failed
echo Setup failed. Check Python 3.12 and the error above.
pause
:end
