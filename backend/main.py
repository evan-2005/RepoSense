import os
import ast
import base64
import asyncio
from collections import defaultdict

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="RepoSense API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GITHUB_API = "https://api.github.com"

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── Pydantic models ──────────────────────────────────────────────────────────

class RepoRequest(BaseModel):
    repo_url: str

class ImpactRequest(BaseModel):
    repo_url: str
    file_path: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_repo_url(url: str) -> tuple[str, str]:
    cleaned = url.strip().rstrip("/")
    for prefix in ["https://github.com/", "http://github.com/", "github.com/"]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    parts = cleaned.split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise HTTPException(400, "Invalid GitHub URL — expected https://github.com/owner/repo")
    return parts[0], parts[1]


async def gh_get(client: httpx.AsyncClient, path: str):
    url = f"{GITHUB_API}{path}"
    resp = await client.get(url, headers=_gh_headers())
    if resp.status_code == 403:
        raise HTTPException(
            403,
            "GitHub rate limit exceeded. Set the GITHUB_TOKEN environment variable "
            "with a personal access token to get 5000 req/hour instead of 60."
        )
    if resp.status_code == 404:
        raise HTTPException(404, f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


async def fetch_tree(client: httpx.AsyncClient, owner: str, repo: str) -> list[dict]:
    """Fetch the entire repo file tree in one API call."""
    data = await gh_get(client, f"/repos/{owner}/{repo}/git/trees/HEAD?recursive=1")
    return data.get("tree", [])


async def fetch_file(client: httpx.AsyncClient, owner: str, repo: str, path: str) -> str:
    """Fetch a single file's decoded text content."""
    data = await gh_get(client, f"/repos/{owner}/{repo}/contents/{path}")
    if isinstance(data, list):
        return ""
    raw = data.get("content", "")
    enc = data.get("encoding", "base64")
    if enc == "base64":
        return base64.b64decode(raw).decode("utf-8", errors="ignore")
    return raw


def extract_imports(source: str) -> list[str]:
    """Return all module names imported in a Python source string."""
    names = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def build_graphs(py_files: list[dict], contents: dict[str, str]):
    """
    Returns (forward_graph, reverse_graph).
    forward[file] = list of files that `file` imports.
    reverse[file] = list of files that import `file`.
    """
    # module_name -> file_path lookup
    mod_map: dict[str, str] = {}
    for f in py_files:
        path = f["path"]
        # e.g. fastapi/routing.py -> fastapi.routing
        mod = path.replace("/", ".").removesuffix(".py")
        mod_map[mod] = path
        # also map the last segment only (e.g. "routing")
        mod_map[path.split("/")[-1].removesuffix(".py")] = path

    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)

    for filepath, source in contents.items():
        for imp in extract_imports(source):
            target = mod_map.get(imp)
            if target and target != filepath:
                forward[filepath].append(target)
                reverse[target].append(filepath)

    return dict(forward), dict(reverse)


def bfs(reverse: dict[str, list[str]], start: str) -> dict[str, int]:
    """BFS through reverse graph; returns {file: hop_distance}."""
    visited: dict[str, int] = {start: 0}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        for dep in reverse.get(cur, []):
            if dep not in visited:
                visited[dep] = visited[cur] + 1
                queue.append(dep)
    visited.pop(start, None)
    return visited


def risk_label(affected: int, total: int) -> str:
    if total == 0:
        return "Unknown"
    pct = affected / total
    if pct > 0.25 or affected > 15:
        return "High"
    if pct > 0.08 or affected > 4:
        return "Medium"
    return "Low"


def tag_file(path: str) -> str:
    name = path.split("/")[-1]
    if name in {"main.py", "app.py", "wsgi.py", "asgi.py",
                "application.py", "applications.py", "__main__.py"}:
        return "entry"
    if name in {"routing.py", "router.py", "urls.py", "routes.py"}:
        return "critical"
    if "test" in path.lower():
        return "test"
    if any(k in path.lower() for k in ("security", "auth", "oauth")):
        return "security"
    return ""


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "project": "RepoSense", "hackathon": "IBM Bob · lablab.ai"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(req: RepoRequest):
    owner, repo = parse_repo_url(req.repo_url)
    async with httpx.AsyncClient(timeout=25) as client:
        meta, tree = await asyncio.gather(
            gh_get(client, f"/repos/{owner}/{repo}"),
            fetch_tree(client, owner, repo),
        )

    all_files = [f for f in tree if f["type"] == "blob"]
    py_files  = [f for f in all_files if f["path"].endswith(".py")]
    dirs      = sorted({
        "/".join(f["path"].split("/")[:-1])
        for f in all_files if "/" in f["path"]
    })

    structured = []
    for item in tree[:300]:
        structured.append({
            "path": item["path"],
            "type": "dir" if item["type"] == "tree" else "file",
            "size": item.get("size", 0),
            "tag": tag_file(item["path"]) if item["type"] == "blob" else "",
        })

    return {
        "owner": owner,
        "repo": repo,
        "description": meta.get("description") or "",
        "language": meta.get("language") or "",
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "total_files": len(all_files),
        "python_files": len(py_files),
        "top_dirs": dirs[:20],
        "tree": structured,
    }


@app.post("/impact")
async def impact(req: ImpactRequest):
    owner, repo = parse_repo_url(req.repo_url)

    async with httpx.AsyncClient(timeout=30) as client:
        tree = await fetch_tree(client, owner, repo)

        py_files = [
            f for f in tree
            if f["type"] == "blob"
            and f["path"].endswith(".py")
            and f.get("size", 9999) < 60_000
        ][:50]  # cap at 50 files to stay within rate limits

        # Fetch all Python files concurrently
        results = await asyncio.gather(
            *[fetch_file(client, owner, repo, f["path"]) for f in py_files],
            return_exceptions=True,
        )

    contents = {
        f["path"]: r
        for f, r in zip(py_files, results)
        if isinstance(r, str) and r
    }

    forward, reverse = build_graphs(py_files, contents)
    affected = bfs(reverse, req.file_path)

    test_files = [f for f in affected if "test" in f.lower()]

    scored = sorted(
        [{"path": p, "hops": h, "level": "high" if h == 1 else "medium" if h == 2 else "low"}
         for p, h in affected.items()],
        key=lambda x: x["hops"],
    )

    return {
        "changed_file": req.file_path,
        "affected_count": len(affected),
        "test_count": len(test_files),
        "direct_imports": len(forward.get(req.file_path, [])),
        "risk": risk_label(len(affected), len(py_files)),
        "affected_files": scored[:40],
    }


@app.post("/guide")
async def guide(req: RepoRequest):
    owner, repo = parse_repo_url(req.repo_url)

    async with httpx.AsyncClient(timeout=25) as client:
        meta, tree = await asyncio.gather(
            gh_get(client, f"/repos/{owner}/{repo}"),
            fetch_tree(client, owner, repo),
        )

        # Try to read README
        readme = ""
        for name in ["README.md", "README.rst", "readme.md", "README.txt"]:
            try:
                readme = await fetch_file(client, owner, repo, name)
                break
            except Exception:
                continue

    all_files  = [f for f in tree if f["type"] == "blob"]
    file_names = {f["path"] for f in all_files}
    py_paths   = [f["path"] for f in all_files if f["path"].endswith(".py")]
    top_dirs   = sorted({f["path"].split("/")[0] for f in all_files if "/" in f["path"]})

    # Detect setup commands
    setup = []
    if "pyproject.toml" in file_names or "setup.py" in file_names:
        setup.append("pip install -e .[dev]")
    if "requirements.txt" in file_names:
        setup.append("pip install -r requirements.txt")
    if "requirements-dev.txt" in file_names:
        setup.append("pip install -r requirements-dev.txt")
    if "package.json" in file_names:
        setup.append("npm install")
    if "Makefile" in file_names:
        setup.append("make help")
    if not setup:
        setup.append("Check README for setup instructions")

    entry_points = [
        p for p in py_paths
        if p.split("/")[-1] in {
            "main.py", "app.py", "wsgi.py", "asgi.py",
            "application.py", "applications.py", "__main__.py"
        }
    ]

    has_tests = any("test" in p.lower() for p in py_paths)

    return {
        "repo": f"{owner}/{repo}",
        "description": meta.get("description") or "No description.",
        "language": meta.get("language") or "Unknown",
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "readme_snippet": readme[:1000] if readme else "No README found.",
        "top_directories": top_dirs[:15],
        "entry_points": entry_points[:6],
        "setup_commands": setup,
        "test_command": "pytest" if has_tests else "No tests detected",
        "total_python_files": len(py_paths),
    }
