@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Compilando Couchpad.exe (tarda un par de minutos)...
python -m pip install -q -r requirements.txt pyinstaller || goto :error
pyinstaller --noconfirm --onefile --noconsole --name Couchpad --icon icon.ico --add-data "index.html;." --add-data "icon.png;." --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --hidden-import pystray._win32 server.py || goto :error
echo.
echo  Listo: dist\Couchpad.exe
echo.
pause
exit /b 0
:error
echo.
echo  Algo ha fallado. Revisa los mensajes de arriba.
pause
exit /b 1
