@echo off
setlocal

set "ROOT=%~dp0.."
pushd "%ROOT%"

if not exist "logs" mkdir "logs"

powershell -ExecutionPolicy Bypass -File "%ROOT%build\build-runner-only.ps1"

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --icon "%ROOT%\assets\runner_icon.ico" ^
  --specpath "%ROOT%\build\specs" ^
  --distpath "%ROOT%" ^
  --name UpdateRunner ^
  "%ROOT%\build\update_runner.py"

popd
endlocal
