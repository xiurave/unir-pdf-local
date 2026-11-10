@echo off
rem Abre Unir PDF. Puedes arrastrar PDFs o imagenes encima de este archivo.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0unir_pdf.py" %*
