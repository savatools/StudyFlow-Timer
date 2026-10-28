@echo off
setlocal
cd /d "%~dp0"
echo Installing dependencies...
py -m pip install --upgrade pip
if errorlevel 1 goto :error
py -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Building StudyFlow Timer.exe ...
py -m PyInstaller --noconfirm --clean --windowed --name StudyFlowTimer --collect-all PySide6 --collect-all shiboken6 --collect-all winotify --collect-all pypresence main.py
if errorlevel 1 goto :error
echo.
echo Done! Your program is here:
echo %~dp0dist\StudyFlowTimer\StudyFlowTimer.exe
echo.
echo The build uses PyInstaller's onedir mode, which is more reliable for Qt WebEngine.
pause
exit /b 0
:error
echo.
echo Something went wrong. Copy the error text and send it to ChatGPT.
pause
exit /b 1
