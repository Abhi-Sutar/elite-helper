@echo off
rem Recce dashboards: elite-dash open | serve | list | new | edit | show | delete | stop
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
"%~dp0..\.venv\Scripts\python.exe" -m eddn_helper.recce %*
