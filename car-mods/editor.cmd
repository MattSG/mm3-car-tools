@echo off
if exist "%~dp0..\..\venv\Scripts\pythonw.exe" ("%~dp0..\..\venv\Scripts\pythonw.exe" "%~dp0launch_editor.py" %*) else (py -3 "%~dp0launch_editor.py" %*)
