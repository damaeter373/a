@echo off
rem Abre un "túnel" USB: el teléfono podrá conectarse a 127.0.0.1:48000 y llegará a esta PC.
rem Requisitos: depuración USB activada en el teléfono y adb (platform-tools) en el PATH.
adb reverse tcp:48000 tcp:48000
pause
