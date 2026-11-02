@echo off
setlocal
title Paotwin - kiem tra
cd /d "%~dp0"
rem Chay bo kiem tra tu dong (khoang 15 giay). Khong can mang, khong mo cong ra ngoai may.

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

echo Python: %PY% %PYARGS%
"%PY%" %PYARGS% -m unittest discover -s tests -t . -v
if errorlevel 1 (echo. & echo CO KIEM TRA HONG, xem dong FAIL/ERROR o tren.) else (echo. & echo Tat ca kiem tra deu dat.)
pause
