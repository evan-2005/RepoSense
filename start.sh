#!/bin/bash
set -e

echo ""
echo "🔬 RepoSense — IBM Bob Hackathon"
echo "================================="
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 not found. Install from https://python.org"
  exit 1
fi

# Install backend deps
echo "▶ Installing backend dependencies..."
cd "$(dirname "$0")/backend"
pip install -r requirements.txt -q

# Start backend
echo "▶ Starting FastAPI backend on http://localhost:8000"
uvicorn main:app --reload --port 8000 --no-access-log &
BACKEND_PID=$!
cd ..

sleep 2

# Open frontend
echo "▶ Opening frontend in browser..."
if command -v xdg-open &>/dev/null; then
  xdg-open frontend/index.html
elif command -v open &>/dev/null; then
  open frontend/index.html
else
  echo "   → Manually open: frontend/index.html"
fi

echo ""
echo "✅ RepoSense is running!"
echo "   Frontend: open frontend/index.html in your browser"
echo "   API:      http://localhost:8000"
echo "   API docs: http://localhost:8000/docs"
echo ""
echo "   Optional: set GITHUB_TOKEN env var to avoid rate limits"
echo "   Press Ctrl+C to stop the backend"
echo ""

trap "kill $BACKEND_PID 2>/dev/null; echo 'Backend stopped.'" EXIT
wait $BACKEND_PID
