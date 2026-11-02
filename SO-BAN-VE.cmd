@echo off
setlocal
title Paotwin - so ban ve
cd /d "%~dp0"
rem Xem so ban ve, ghi xac minh, chon bo ban ve ban sao dung.

rem --- tim Python: uu tien moi truong rieng .venv (co thu vien STEP), roi Python portable tren o, roi py.exe ---
set "PY="
set "PYARGS="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
for /d %%R in ("%~d0\Python\runtimes\cpython-3*") do if not defined PY if exist "%%R\python.exe" set "PY=%%R\python.exe"
if not defined PY (
  for /f "delims=" %%P in ('where py.exe 2^>nul') do if not defined PY set "PY=%%P"
  if defined PY set "PYARGS=-3"
)
if not defined PY (
  echo Khong tim thay Python. Can thu muc %~d0\Python\runtimes tren o, hoac Python cai tren may.
  pause
  exit /b 1
)
"%PY%" %PYARGS% -c "import sys; sys.exit(sys.version_info < (3, 10))"
if errorlevel 1 (
  echo Can Python 3.10 tro len. Python tim thay: %PY%
  pause
  exit /b 1
)

:menu
echo.
"%PY%" %PYARGS% -m paotwin.ban_ve xem
echo.
echo   1. Ghi xac minh (da doi chieu voi may that)
echo   2. Chon bo ban ve cho ban sao dung
echo   3. Thoat
set "CHON="
set /p "CHON=Chon 1, 2 hoac 3: "
if "%CHON%"=="1" goto xacminh
if "%CHON%"=="2" goto dung
goto :eof

:xacminh
set "MA="
set "NGUOI="
set "GHICHU="
set /p "MA=Ma bo ban ve (vd BT-001-A): "
set /p "NGUOI=Ten nguoi da doi chieu: "
set /p "GHICHU=Da doi chieu nhung gi: "
"%PY%" %PYARGS% -m paotwin.ban_ve xac-minh "%MA%" --nguoi "%NGUOI%" --ghi-chu "%GHICHU%"
goto menu

:dung
set "MA="
set /p "MA=Ma bo ban ve, hoac tham-khao de quay ve mo hinh lam tay: "
"%PY%" %PYARGS% -m paotwin.ban_ve dung "%MA%"
echo Dong demo dang chay (neu co) roi bam CHAY-DEMO.cmd de thay doi.
goto menu
