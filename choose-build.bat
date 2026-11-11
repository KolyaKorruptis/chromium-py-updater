@echo off
setlocal
rem Opens the build picker, to switch Chromium-Py-Updater to another Chromium build.

set "PYTHON=python"
where py >nul 2>nul && set "PYTHON=py"

rem This window belongs to the picker, so it may be resized to fit.
set "CPL_OWN_CONSOLE=1"
%PYTHON% "%~dp0Chromium.py" --choose-build
rem Python not found: keep the message from cmd readable.
if %errorlevel% equ 9009 pause
