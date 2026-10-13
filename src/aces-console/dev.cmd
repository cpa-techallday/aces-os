@echo off
rem ACES console preview entry for `npm run dev` (args after -- are forwarded).
setlocal
set "PY311=C:\Users\shedm\AppData\Local\Programs\Python\Python311\python.exe"
if exist "%PY311%" (set "PY=%PY311%") else (set "PY=python")
"%PY%" "%~dp0dev_server.py" %*
exit /b %errorlevel%
