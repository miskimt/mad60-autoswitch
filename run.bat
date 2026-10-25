@echo off
cd /d "%~dp0"
where py >nul 2>nul && (py -3 kb_autoswitch.py) || (python kb_autoswitch.py)
pause
