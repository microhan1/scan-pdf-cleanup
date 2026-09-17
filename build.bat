@echo off
rem Build scan-pdf-cleanup.exe (single file, no console) with PyInstaller.
rem Usage: build.bat          -> dist\scan-pdf-cleanup.exe
setlocal
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>&1 || python -m pip install pyinstaller
python -m pip install -r requirements.txt

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name scan-pdf-cleanup ^
  --add-data "lang;lang" ^
  --collect-data tkinterdnd2 ^
  main.py

if errorlevel 1 (
  echo Build failed.
  exit /b 1
)
echo.
echo Done: dist\scan-pdf-cleanup.exe
endlocal
