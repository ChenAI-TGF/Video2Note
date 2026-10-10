@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [启动失败] 没有找到 python 命令。
    echo 请先安装 Python 3.10+，安装时勾选 "Add python.exe to PATH"，然后重新双击 setup.bat。
    pause
    exit /b 1
)

echo ============================================
echo  正在打开配置向导（检查/安装依赖、配置 Cookie 与 API）
echo ============================================
echo.
python setup_ui.py
if errorlevel 1 (
    echo.
    echo [提示] 配置向导意外退出。
    echo 若界面没有弹出，请确认当前 Python 自带 tkinter（官方安装包默认自带）。
    echo 也可以手动安装依赖：python -m pip install -r requirements.txt
    pause
)
