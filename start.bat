@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 正在创建 Python 虚拟环境...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>&1
        if errorlevel 1 goto :missing_python
        python -m venv .venv
    )
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, httpx, jinja2, keyring" >nul 2>&1
if errorlevel 1 (
    echo 正在安装项目依赖，首次运行可能需要一段时间...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

".venv\Scripts\python.exe" main.py %*
set "exit_code=%errorlevel%"
if not "%exit_code%"=="0" pause
exit /b %exit_code%

:missing_python
echo 未找到 Python。请先安装 Python 3.11 或更新版本，并勾选加入 PATH。
pause
exit /b 1

:setup_failed
echo 环境准备失败，请检查 Python 安装和网络连接后重试。
pause
exit /b 1
