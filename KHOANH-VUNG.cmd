@echo off
setlocal
title Paotwin - khoanh vung
cd /d "%~dp0"
rem Mo cong cu khoanh vung: bam chon chi tiet tren hinh 3D cua mot ban ve STEP va gan vao vung cua may.
rem Cach dung: keo tha tep .step / .stp len tep nay, hoac bam dup roi chon tep trong hop thoai.
rem Ghi vao ban-ve\may\<ma may>\ (khong len git). Can cai thu vien STEP truoc: CAI-DAT-STEP.cmd.

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

set "FILE=%~1"
if not "%FILE%"=="" goto co_tep
echo Chon tep ban ve STEP trong hop thoai...
for /f "usebackq delims=" %%F in (`powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Title = 'Chon ban ve STEP'; $d.Filter = 'Ban ve STEP (*.step, *.stp)' + [char]124 + '*.step;*.stp'; if ($d.ShowDialog() -eq 'OK') { $d.FileName }"`) do set "FILE=%%F"
if "%FILE%"=="" (
  echo Chua chon tep nao.
  pause
  exit /b 1
)

:co_tep
echo Ban ve: %FILE%
echo.
echo Ma may: chu thuong khong dau, so, - hoac _ (vd bts7960). Dung lai ma cu de sua tiep.
echo Ma ban-thu la ban thu cua demo (vung co san, khong them bot).
set "MA="
set /p "MA=Ma may: "
"%PY%" %PYARGS% -m paotwin.khoanh_vung "%FILE%" --may "%MA%"
echo.
pause
