@echo off
echo.
echo 🔬 RepoSense — IBM Bob Hackathon
echo =================================
echo.

:: Install backend deps
echo Installing backend dependencies...
cd backend
pip install -r requirements.txt -q
if errorlevel 1 (
    echo ERROR: pip install failed. Make sure Python is installed.
    pause
    exit /b 1
)

:: Start backend in new window
echo Starting FastAPI backend on http://localhost:8000 ...
start "RepoSense Backend" cmd /k "uvicorn main:app --reload --port 8000"
cd ..

timeout /t 3 /nobreak >nul

:: Open frontend
echo Opening frontend in browser...
start "" "frontend\index.html"

echo.
echo ✅ RepoSense is running!
echo    Frontend: frontend\index.html (opened in browser)
echo    API:      http://localhost:8000
echo    API docs: http://localhost:8000/docs
echo.
echo    Optional: set GITHUB_TOKEN env var to avoid rate limits
echo    Close the backend window to stop the server.
echo.
pause
