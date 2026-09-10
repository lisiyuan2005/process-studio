@echo off
chcp 65001 >nul
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"
python -c "import numpy, scipy, skfmm, matplotlib, gdstk, openpyxl, PIL" >nul 2>&1
if errorlevel 1 (
  echo 正在安装 Process Studio 依赖，请稍候...
  python -m pip install -e .
  if errorlevel 1 goto :failed
)
set "PYTHONPATH=%APP_DIR%src"
python -m process_studio --workspace "%APP_DIR%workspace"
if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo 启动失败。请确认已安装 Python 3.11 或更高版本。
pause
exit /b 1
