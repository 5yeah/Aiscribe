@echo off
chcp 65001 >nul
setlocal
rem 一键周报：生成 reports\AI周报_日期.md 并打开所在文件夹。
rem 本脚本位于 scripts\，仓库根目录在上一级（%~dp0..）。
rem PY：默认用 PATH 里的 python；本机有专属解释器时把绝对路径写在这里。
set "PY=python"
pushd "%~dp0.."
"%PY%" -m ai_report weekly %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  popd
  echo 周报生成失败（退出码 %RC%）
  pause
  exit /b %RC%
)
start "" "%CD%\reports"
popd
endlocal
