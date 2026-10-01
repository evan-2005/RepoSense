# 🔬 RepoSense — IBM Bob Hackathon

> **lablab.ai · IBM Bob Hackathon · May 15–17, 2026**

Paste any public GitHub repo URL → instantly get:
- **Codebase map** — full file tree tagged with entry points, critical files, security modules
- **Impact analysis** — real Python AST import graph + BFS blast radius of any change
- **Onboarding guide** — auto-generated setup steps, entry points, README excerpt

---

## 🚀 Quick start (2 terminals)

### Terminal 1 — Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

✅ API running at http://localhost:8000  
✅ Swagger docs at http://localhost:8000/docs

### Terminal 2 — Frontend

Just open the file in your browser — **no build step needed**:

```bash
# Mac
open frontend/index.html

# Windows
start frontend/index.html

# Linux
xdg-open frontend/index.html
```

Or drag `frontend/index.html` into any browser tab.

> **Windows one-click:** double-click `start.bat`  
> **Mac/Linux one-click:** run `./start.sh`

---

## ⚠️ GitHub Rate Limit

Without a token, GitHub allows **60 API requests/hour**.  
To get **5,000/hour**, create a free token:

1. Go to https://github.com/settings/tokens → **Generate new token (classic)**
2. No scopes needed for public repos — just generate it
3. Set it in the backend before starting:

```bash
# Mac/Linux
export GITHUB_TOKEN=ghp_your_token_here
uvicorn main:app --reload --port 8000

# Windows PowerShell
$env:GITHUB_TOKEN="ghp_your_token_here"
uvicorn main:app --reload --port 8000

# Windows CMD
set GITHUB_TOKEN=ghp_your_token_here
uvicorn main:app --reload --port 8000
```

---

## 📁 Project structure

```
reposense/
├── backend/
│   ├── main.py           # FastAPI — /analyze, /impact, /guide endpoints
│   └── requirements.txt  # fastapi, uvicorn, httpx, pydantic
├── frontend/
│   └── index.html        # Complete UI — single file, no build needed
├── bob-session-export.json
├── start.bat             # Windows one-click launcher
├── start.sh              # Mac/Linux one-click launcher
└── README.md
```

---

## 🛠 How impact analysis works

1. Fetch the repo's **default branch** tree via GitHub API
2. Filter Python files (`.py`), skip files > 60 KB, cap at 50 files to stay within rate limits
3. Parse each with Python's `ast` module — extract `import` and `from X import` statements
4. Build a directed graph: `file → [files it imports]`
5. Reverse it: `file → [files that import IT]`
6. BFS from the changed file through the reverse graph (O(1) dequeue via `collections.deque`)
7. Score each affected file by hop distance (1 hop = direct importer → High risk)

---

## 📋 API endpoints

```
GET  /           → health check
POST /analyze    { "repo_url": "https://github.com/owner/repo" }
POST /impact     { "repo_url": "...", "file_path": "fastapi/routing.py" }
POST /guide      { "repo_url": "https://github.com/owner/repo" }
```

---

## 🐛 Bug fixes (post-hackathon)

| # | Location | Bug | Fix |
|---|----------|-----|-----|
| 1 | `backend/main.py` | `fetch_tree` used hardcoded `HEAD` as tree SHA — GitHub API rejects this for many repos | Now reads `default_branch` from repo metadata and passes it to the tree API |
| 2 | `backend/main.py` | `bfs()` used `list.pop(0)` — O(n) per dequeue, quadratic slowdown on large repos | Replaced with `collections.deque` + `popleft()` for O(1) performance |
| 3 | `frontend/index.html` | File paths and tag names injected raw into `innerHTML` — XSS risk | All user-supplied values now escaped via `escHtml()` before injection |
| 4 | `frontend/index.html` | `escHtml()` didn't escape `"` — quotes in paths could break HTML attributes | Added `"` → `&quot;` and coerced input with `String(s)` |

---

## 🤖 Built with IBM Bob

IBM Bob was used throughout to design the AST traversal, BFS algorithm, and FastAPI endpoint architecture. See `bob-session-export.json` for the session log.

> **Before submitting:** replace `bob-session-export.json` with your actual IBM Bob VS Code export.

