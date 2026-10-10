@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo [启动失败] 没有找到 python 命令。
    echo 请先安装 Python 3.10+，安装时勾选 "Add python.exe to PATH"，然后重新双击 run.bat。
    pause
    exit /b 1
)

rem ---- 依赖自检：缺失就先打开配置向导去安装，避免中途才报错 ----
python check_deps.py >nul 2>nul
if errorlevel 1 (
    echo ============================================================
    echo  检测到依赖不完整，先打开配置向导进行安装...
    echo  在「系统配置」里点「安装缺失依赖」，完成后再次双击 run.bat。
    echo ============================================================
    echo.
    python setup_ui.py
    echo.
    echo 安装 / 配置完成后，请再次双击 run.bat 启动程序。
    pause
    exit /b 0
)

rem ---- 首次使用（还没有 config.json）也先走配置向导 ----
if not exist "config.json" (
    echo ============================================================
    echo  首次使用，先打开配置向导完成基本配置（Cookie / API）...
    echo  配置完成后，请再次双击 run.bat 启动程序。
    echo ============================================================
    echo.
    python setup_ui.py
    echo.
    pause
    exit /b 0
)

python -m app.main
if errorlevel 1 (
    echo.
    echo [启动失败] 请查看上方错误信息；依赖或配置问题可双击 setup.bat 打开配置向导修复。
    pause
)
