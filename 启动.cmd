@echo off
chcp 65001 >nul 2>nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
title 小说按字数切分

rem ================= 找一个能用的 Python =================
set "PY="
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY goto no_python

rem ================= 开始切分 =================
rem  把书拖到本文件上时，文件路径会作为参数传进来（%*）
rem  没拖文件直接双击，则进入交互式提问
%PY% "%~dp0novel_split.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" echo   [OK] 切分完成。结果就在上面「输出目录」指的那个文件夹里。
if "%RC%"=="1" echo   [错误] 出错了，原因见上面的说明。
if "%RC%"=="2" echo   [取消] 没有拿到文件。
if "%RC%"=="3" echo   [无法切分] 这本书切不了，原因见上面的说明。
echo.
echo   按任意键关闭本窗口...
pause >nul
exit /b %RC%

:no_python
echo.
echo   [错误] 没有找到 Python，没法运行。
echo.
echo       这个工具需要 Python 3.10 或更新的版本：
echo         1. 打开 https://www.python.org/downloads/ 下载
echo         2. 安装时务必勾选 "Add python.exe to PATH"
echo         3. 装好之后，再双击一次本文件
echo.
echo   按任意键关闭本窗口...
pause >nul
exit /b 1
