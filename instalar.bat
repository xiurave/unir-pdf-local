@echo off
rem Prepara Unir PDF: crea un entorno de Python aqui mismo e instala lo necesario.
rem Solo hace falta correrlo una vez (necesita internet para descargar las librerias).
cd /d "%~dp0"
py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo No se encontro Python. Instala Python 3.11 o mas reciente desde https://www.python.org
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo Listo. Ya puedes abrir UnirPDF.bat
pause
