@echo off
title IDX Smart Screener — Django Dashboard
echo.
echo  =====================================================
echo    IDX SMART SCREENER — Django Edition
echo    Smart Money Concept + Foreign Flow Analysis
echo  =====================================================
echo.

:: Detect Python — try full known path first, then common aliases
set PYTHON=
if exist "C:\Users\Nitro\.local\bin\python3.14.exe" (
    set PYTHON=C:\Users\Nitro\.local\bin\python3.14.exe
    goto :found
)
where python3 >nul 2>&1
if not errorlevel 1 ( set PYTHON=python3 & goto :found )
where python >nul 2>&1
if not errorlevel 1 ( set PYTHON=python  & goto :found )
where py >nul 2>&1
if not errorlevel 1 ( set PYTHON=py      & goto :found )
echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
pause
exit /b 1

:found
echo [OK] Using Python: %PYTHON%
%PYTHON% --version
echo.

:: Create virtual environment if missing
if not exist "venv\Scripts\activate.bat" (
    echo [1/4] Creating virtual environment...
    %PYTHON% -m venv venv
) else (
    echo [1/4] Virtual environment found.
)

:: Activate
echo [2/4] Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
echo [3/4] Installing dependencies...
pip install -r requirements.txt --quiet

:: Open browser after 3-second delay
echo [4/4] Starting Django server on http://127.0.0.1:8000 ...
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:8000"

:: Launch Django
python manage.py runserver 8000

pause
