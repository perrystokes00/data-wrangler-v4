@echo off
title DataView v3
echo ============================================
echo  DataView v3
echo ============================================
echo.

cd /d C:\Users\perry\OneDrive\Documents\PPDM\claude_use_ai\data_wrangler\data_wrangler_clean

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo Python:
python --version
echo Streamlit:
python -m streamlit --version

echo.
echo Starting...
echo.

python -m streamlit run app_v4.py --server.port 8502

echo.
echo Server stopped.
pause
