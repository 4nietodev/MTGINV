@echo off
setlocal
title MTG Collection and Deck Manager - Installer

echo ========================================================
echo    MTG Collection and Deck Manager - Installer
echo ========================================================
echo.

:: Step 0: Check if virtual environment already exists and works
if not exist .venv\Scripts\python.exe goto DETECT_PYTHON
.venv\Scripts\python.exe --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] Existing virtual environment found [.venv].
    goto INSTALL_DEPS
)

:DETECT_PYTHON
echo [1/3] Detecting Python installation...
set "PY_CMD="

:: Check py launcher
py -0 >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=py"
    goto PYTHON_FOUND
)

:: Check python in PATH
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=python"
    goto PYTHON_FOUND
)

:: Check common installation paths
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto PYTHON_FOUND
)
if exist "%ProgramFiles%\Python312\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python312\python.exe"
    goto PYTHON_FOUND
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto PYTHON_FOUND
)
if exist "%ProgramFiles%\Python311\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python311\python.exe"
    goto PYTHON_FOUND
)
if exist "%LOCALAPPDATA%\anaconda3\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\anaconda3\python.exe"
    goto PYTHON_FOUND
)
if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PY_CMD=%USERPROFILE%\anaconda3\python.exe"
    goto PYTHON_FOUND
)
if exist "%LOCALAPPDATA%\miniconda3\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\miniconda3\python.exe"
    goto PYTHON_FOUND
)
if exist "%USERPROFILE%\miniconda3\python.exe" (
    set "PY_CMD=%USERPROFILE%\miniconda3\python.exe"
    goto PYTHON_FOUND
)

:: Step 1.5: Automatic Python installation
echo.
echo [WARNING] Python was not found on your system.
echo [INFO] Starting automatic Python 3.12 installation...
echo.

:: Attempt 1: Windows Package Manager (winget)
where winget >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto TRY_POWERSHELL_INSTALL

echo [INFO] Installing Python 3.12 via winget...
winget install --id Python.Python.3.12 --exact --silent --accept-package-agreements --accept-source-agreements

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto PYTHON_FOUND
)
if exist "%ProgramFiles%\Python312\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python312\python.exe"
    goto PYTHON_FOUND
)
py -0 >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=py"
    goto PYTHON_FOUND
)
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=python"
    goto PYTHON_FOUND
)

:TRY_POWERSHELL_INSTALL
:: Attempt 2: Download official Python 3.12 installer and run silent setup
set "PY_ARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "PY_ARCH=arm64"
set "PY_URL=https://www.python.org/ftp/python/3.12.9/python-3.12.9-%PY_ARCH%.exe"
set "TEMP_INSTALLER=%TEMP%\python_installer_312.exe"

where curl >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [INFO] Downloading official Python 3.12 installer via curl...
    curl -fSLo "%TEMP_INSTALLER%" "%PY_URL%"
) else (
    echo [INFO] Downloading official Python 3.12 installer via PowerShell...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('%PY_URL%', $env:TEMP_INSTALLER)"
)

if not exist "%TEMP_INSTALLER%" goto INSTALL_FAILED

echo [INFO] Running silent Python installation...
"%TEMP_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 SimpleInstall=1
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Python installer finished with exit code %ERRORLEVEL%.
)
del "%TEMP_INSTALLER%" >nul 2>&1

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto PYTHON_FOUND
)
if exist "%ProgramFiles%\Python312\python.exe" (
    set "PY_CMD=%ProgramFiles%\Python312\python.exe"
    goto PYTHON_FOUND
)
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
    goto PYTHON_FOUND
)
py -0 >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=py"
    goto PYTHON_FOUND
)
python --version >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=python"
    goto PYTHON_FOUND
)

:INSTALL_FAILED
echo.
echo [ERROR] Automatic Python installation could not be completed.
echo Please check your internet connection or install Python manually from https://www.python.org/downloads/
pause
exit /b 1

:PYTHON_FOUND
echo [INFO] Using Python: %PY_CMD%
"%PY_CMD%" --version

:: Step 2: Create virtual environment (.venv)
if exist .venv goto VENV_READY
echo [2/3] Creating virtual environment [.venv]...
"%PY_CMD%" -m venv .venv
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Failed to create virtual environment [.venv].
    pause
    exit /b 1
)
echo Virtual environment created successfully.

:VENV_READY
echo [2/3] Virtual environment [.venv] ready.

:INSTALL_DEPS
:: Step 3: Upgrade pip and install requirements into .venv
echo [3/3] Installing dependencies from requirements.txt into .venv...
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Failed to install dependencies. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo    Installation completed successfully!
echo    You can now double-click run.bat to start the app.
echo ========================================================
echo.
pause
