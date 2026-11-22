@echo off
rem Instala las dependencias la primera vez y abre PhoneMix.
cd /d "%~dp0"
python -c "import sounddevice, numpy, pycaw" >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias...
    python -m pip install -r requirements.txt
)
start "" pythonw phonemix.py
