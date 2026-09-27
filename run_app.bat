@echo off
rem Starts the prediction app and opens it in the browser (http://localhost:8501).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m streamlit run app.py
pause
