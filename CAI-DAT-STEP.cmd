@echo off
setlocal
title Paotwin - cai thu vien doc STEP
cd /d "%~dp0"
rem Cai cadquery-ocp-novtk 8.0.1.1.0 (OpenCASCADE, khoang 47,5 MB) vao moi truong rieng .venv
rem cua du an nay. Khong dung toi Python dung chung. Can mang Internet. Chi can chay mot lan.
rem Xong thi tu chay bai kiem tra STEP (tao tep STEP mau, doc lai, so ket qua).

rem --- tim Python: Python portable tren o, roi py.exe (de tao .venv) ---
set "PY="
set "PYARGS="
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

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Tao moi truong rieng .venv ...
  "%PY%" %PYARGS% -m venv "%~dp0.venv"
  if errorlevel 1 (echo Khong tao duoc .venv. & pause & exit /b 1)
)
set "VPY=%~dp0.venv\Scripts\python.exe"
echo Cai cadquery-ocp-novtk==8.0.1.1.0 ...
"%VPY%" -m pip install --disable-pip-version-check "cadquery-ocp-novtk==8.0.1.1.0"
if errorlevel 1 (echo. & echo Cai KHONG thanh cong, xem loi o tren. & pause & exit /b 1)
echo.
echo Tu kiem tra phan doc STEP ...
"%VPY%" -m unittest tests.test_step -v
if errorlevel 1 (echo. & echo CO KIEM TRA HONG, xem dong FAIL/ERROR o tren.) else (echo. & echo Doc STEP hoat dong. Ket qua: nhat-ky\tu-kiem-tra-step.json)
pause
