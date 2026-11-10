@echo off
rem 看后台日志（不乱码）。双击 = 菜单；也可以：看日志.cmd 重载 30
rem 环境变量：PERSONA_PY = Python 解释器（不设就试 %ProgramData%\SysPersona-Runtime，再退到 PATH 上的 python）。
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY=%PERSONA_PY%"
if not defined PY if exist "%ProgramData%\SysPersona-Runtime\python\python.exe" set "PY=%ProgramData%\SysPersona-Runtime\python\python.exe"
if not defined PY set "PY=python"

if "%~1"=="" goto menu
"%PY%" -X utf8 "%HERE%persona_logs.py" %*
echo.
pause
exit /b %ERRORLEVEL%

:menu
"%PY%" -X utf8 "%HERE%persona_logs.py" -i
exit /b 0
