@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
if errorlevel 1 goto failed
python "%~dp0main.py"
set "APERTUS_LAUNCH_EXIT=%ERRORLEVEL%"
popd
if "%APERTUS_LAUNCH_EXIT%"=="0" exit /b 0
:failed
echo.
echo Apertus Mundus could not start.
echo Please check Python and run: python -m pip install -r "%~dp0requirements.txt"
pause
exit /b 1
