@echo off
REM ============================================================
REM  启动本地运行时语言服务（Ollama），供第4周自然语言助手使用
REM
REM  为什么需要这个脚本，而不是直接开 Ollama 托盘程序：
REM    Ollama 0.40.3 的托盘程序启动服务时，读到的模型目录是
REM    C:\Users\user\.ollama\models（那是指向 D:\ollama\models 的 junction），
REM    而在 junction 路径下它解析不到模型清单 —— /api/tags 返回空，
REM    日志里刷 bad manifest: The system cannot find the path specified。
REM    结果是模型"消失"，助手的意图解析全部失败。
REM    用**显式**的 D:\ollama\models 启动就正常。
REM
REM  所以：先停掉托盘服务，再用显式路径启动。
REM ============================================================

echo [1/3] 停止托盘启动的 Ollama 进程...
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM "ollama.exe" /F >nul 2>&1
timeout /t 3 /nobreak >nul

echo [2/3] 用显式模型目录启动服务: D:\ollama\models
set OLLAMA_MODELS=D:\ollama\models
start "ollama-serve" /MIN "C:\Users\user\AppData\Local\Programs\Ollama\ollama.exe" serve

echo [3/3] 等待服务就绪...
timeout /t 10 /nobreak >nul

echo.
echo 检查模型列表（应能看到 qwen3.8-q2 等）：
curl -s --noproxy "*" http://127.0.0.1:11434/api/tags
echo.
echo.
echo 如果上面是 {"models":[]}，说明模型目录又不对，检查：
echo   - D:\ollama\models\manifests-v2\ 下有没有清单
echo   - 环境变量 OLLAMA_MODELS 是否为 D:\ollama\models
pause
