@echo off
rem 看后台日志（不乱码）。双击 = 菜单；也可以：看日志.cmd 重载 30
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY=D:\ProgramData\SysPersona-Runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1"=="" goto menu
"%PY%" -X utf8 "%HERE%persona_logs.py" %*
echo.
pause
exit /b %ERRORLEVEL%

:menu
"%PY%" -X utf8 "%HERE%persona_logs.py" -i
exit /b 0
