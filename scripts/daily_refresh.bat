@echo off
rem Daily rep-record maintenance (#5863). Task Scheduler runs this at 03:30.
rem Machine-specific settings live in scripts\local_env.bat (gitignored); see local_env.example.bat.
setlocal
cd /d "%~dp0.."
if exist "%~dp0local_env.bat" call "%~dp0local_env.bat"
if not defined RESISTOR_PYTHON set "RESISTOR_PYTHON=python"
set "PYTHONIOENCODING=utf-8"
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "TODAY=%%d"
if not exist data\logs mkdir data\logs
set "LOG=data\logs\daily_refresh-%TODAY%.log"
echo ==== daily refresh start %DATE% %TIME% >> "%LOG%"

echo --- refresh %TIME% >> "%LOG%"
"%RESISTOR_PYTHON%" -m src.rep_record refresh >> "%LOG%" 2>&1
echo exit %ERRORLEVEL% >> "%LOG%"

echo --- backfill-crec (this year + 2 older per night) %TIME% >> "%LOG%"
"%RESISTOR_PYTHON%" -m src.rep_record backfill-crec --max-years 3 >> "%LOG%" 2>&1
echo exit %ERRORLEVEL% >> "%LOG%"

echo --- backfill-votes %TIME% >> "%LOG%"
"%RESISTOR_PYTHON%" -m src.rep_record backfill-votes >> "%LOG%" 2>&1
echo exit %ERRORLEVEL% >> "%LOG%"

if defined RESISTOR_YOUTUBE_ROOT (
  echo --- youtube captions %TIME% >> "%LOG%"
  "%RESISTOR_PYTHON%" -m src.rep_youtube >> "%LOG%" 2>&1
  echo exit %ERRORLEVEL% >> "%LOG%"
) else (
  echo --- youtube skipped: RESISTOR_YOUTUBE_ROOT not set >> "%LOG%"
)

rem Media keeps its keyboard-idle gate; plan-billed Haiku, this year + 2 older per rep, capped.
echo --- media %TIME% >> "%LOG%"
"%RESISTOR_PYTHON%" -m src.rep_media --backend cli --max-years 3 --budget-usd 1.0 >> "%LOG%" 2>&1
echo exit %ERRORLEVEL% >> "%LOG%"

echo --- progress summary %TIME% >> "%LOG%"
"%RESISTOR_PYTHON%" -m src.rep_record progress 2>&1 | findstr /C:"all sources" >> "%LOG%"
echo ==== daily refresh end %DATE% %TIME% >> "%LOG%"
endlocal
