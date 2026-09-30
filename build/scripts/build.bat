@echo off
setlocal

set "ROOT=%~dp0..\.."
pushd "%ROOT%"

powershell -ExecutionPolicy Bypass -File "%ROOT%build\scripts\build-runner-only.ps1"

python -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --icon "%ROOT%\src\assets\runner_icon.ico" ^
  --specpath "%ROOT%\build\temp\specs" ^
  --distpath "%ROOT%\dist\windows" ^
  --name UpdateRunner ^
  "%ROOT%\build\scripts\update_runner.py"

popd
endlocal
