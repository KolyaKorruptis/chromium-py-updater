@echo off
setlocal
rem Registers Chromium.py with Windows, so it can be picked as the default browser.

set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py"

echo Chromium-Py-Updater as default browser
echo.
echo   1  Register, then choose it in the Windows settings that open
echo   2  Remove the registration
echo.
set "CHOICE="
set /p "CHOICE=Choose 1 or 2: "
echo.

if "%CHOICE%"=="1" (
    %PYTHON% "%~dp0Chromium.py" --register
) else if "%CHOICE%"=="2" (
    %PYTHON% "%~dp0Chromium.py" --unregister
) else (
    echo Nothing changed.
)

echo.
pause
