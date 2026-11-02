@echo off
setlocal
title Paotwin - nhap ban ve
cd /d "%~dp0"
rem Nhap mot ban ve DXF vao so ban ve (thanh Candidate) va cho ban sao dung no.
rem Cach dung: keo tha tep .dxf hoac .step len tep nay. Bam dup khong keo gi: nhap ban ve mau DXF.
rem Tep STEP can cai thu vien truoc: CAI-DAT-STEP.cmd.

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

set "FILE=%~1"
if "%FILE%"=="" set "FILE=%~dp0ban-ve\mau\BT-001-A.dxf"
echo Ban ve: %FILE%
echo.
set "EXT=%~x1"
if /i "%EXT%"==".step" goto step
if /i "%EXT%"==".stp" goto step
"%PY%" %PYARGS% -m paotwin.ban_ve nhap "%FILE%" --dung
goto xong

:step
rem Tep STEP khong co khung ten doc duoc: hoi so ban ve va revision.
set "SO="
set "REV="
set "ANHXA="
set /p "SO=So ban ve: "
set /p "REV=Revision (vd A): "
set /p "ANHXA=Tep anh xa ten chi tiet -> vung (.json), bo trong neu khong co: "
if "%ANHXA%"=="" (
  "%PY%" %PYARGS% -m paotwin.ban_ve nhap "%FILE%" --so-ban-ve "%SO%" --revision "%REV%" --dung
) else (
  "%PY%" %PYARGS% -m paotwin.ban_ve nhap "%FILE%" --so-ban-ve "%SO%" --revision "%REV%" --anh-xa "%ANHXA%" --dung
)

:xong
echo.
echo Dong demo dang chay (neu co) roi bam CHAY-DEMO.cmd de thay ban ve moi.
pause
