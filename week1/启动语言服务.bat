@echo off
chcp 65001 >nul
REM ============================================================
REM  启动本地运行时语言服务（Ollama），供第 4 周自然语言助手 / 第 8 周视觉推理使用
REM
REM  为什么需要这个脚本，而不是直接开 Ollama 托盘程序：
REM    Ollama 0.40.x 的托盘程序读到的模型目录是 %USERPROFILE%\.ollama\models，
REM    它常被做成指向别处的目录联接（junction）。在 junction 路径下 Ollama
REM    解析不到模型清单 —— /api/tags 返回 {"models":[]}，
REM    日志里刷 bad manifest: The system cannot find the path specified。
REM    表现是"模型全没了、助手全部解析失败"，看着像代码坏了，其实是路径。
REM    用**显式**的 OLLAMA_MODELS 启动就正常。
REM
REM  所以：先停掉托盘服务，再用显式路径启动。
REM  模型目录默认 D:\ollama\models，可用环境变量 OLLAMA_MODELS 覆盖。
REM ============================================================

setlocal

REM 找 ollama.exe：先按默认安装位置，再退回 PATH
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" (
  for /f "delims=" %%i in ('where ollama.exe 2^>nul') do set "OLLAMA_EXE=%%i"
)
if not exist "%OLLAMA_EXE%" (
  echo [x] 没找到 ollama.exe
  echo     找过: %LOCALAPPDATA%\Programs\Ollama\ollama.exe 和 PATH
  echo     请先安装 Ollama，或把本脚本的 OLLAMA_EXE 改成实际路径
  pause
  exit /b 1
)

REM 模型目录：没设就用默认值，设了但不存在就报错，不猜
if not defined OLLAMA_MODELS set "OLLAMA_MODELS=D:\ollama\models"
if not exist "%OLLAMA_MODELS%" (
  echo [x] 模型目录不存在: %OLLAMA_MODELS%
  echo     模型放在别处的话，先 set OLLAMA_MODELS=... 再重跑本脚本
  pause
  exit /b 1
)

echo [1/3] 停止托盘启动的 Ollama 进程...
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM "ollama.exe" /F >nul 2>&1
timeout /t 3 /nobreak >nul

echo [2/3] 用显式模型目录启动服务: %OLLAMA_MODELS%
start "ollama-serve" /MIN "%OLLAMA_EXE%" serve

echo [3/3] 等待服务就绪...
timeout /t 10 /nobreak >nul

echo.
echo 检查模型列表（应能看到 qwen3.8-q2 / qwen2.5vl:3b 等）:
curl -s --noproxy "*" http://127.0.0.1:11434/api/tags
echo.
echo.
echo 如果上面是 {"models":[]}，说明模型目录又不对，检查:
echo   - %OLLAMA_MODELS%\manifests-v2\ 下有没有清单
echo   - 环境变量 OLLAMA_MODELS 是否指向真实模型目录
pause
