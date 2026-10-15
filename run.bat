@echo off
setlocal
title MTG Collection and Deck Manager

echo ========================================================
echo    Starting MTG Collection and Deck Manager...
echo ========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [WARNING] Virtual environment not found. Running installer first...
    call install.bat
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Installation failed or aborted.
        pause
        exit /b 1
    )
)

echo Launching Streamlit web application...
".venv\Scripts\python.exe" -m streamlit run app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Streamlit exited with an error. Check mtg_app.log for details.
    pause
)
