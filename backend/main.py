import os
import re
import time
import base64
import asyncio
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from import_graph import build_graphs, bfs

app = FastAPI(title="RepoSense API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"

TREE_DISPLAY_LIMIT = 300
MAX_IMPACT_FILES = 400       # raw downloads don't count against the API rate limit
MAX_PY_FILE_BYTES = 200_000
RAW_CONCURRENCY = 16
CACHE_TTL_SECONDS = 600

ENTRY_NAMES = {"main.py", "app.py", "wsgi.py", "asgi.py",
               "application.py", "applications.py", "__main__.py", "manage.py"}
CRITICAL_NAMES = {"routing.py", "router.py", "urls.py", "routes.py"}

_OWNER_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}$"), re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_repo_cache: dict[tuple[str, str], tuple[float, dict, list[dict]]] = {}


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


# ── Pydantic models ──────────────────────────────────────────────────────────

class RepoRequest(BaseModel):
    repo_url: str = Field(..., max_length=300)

class ImpactRequest(BaseModel):
    repo_url: str = Field(..., max_length=300)
    file_path: str = Field(..., max_length=1000)


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_repo_url(url: str) -> tuple[str, str]:
    """Accepts https://github.com/o/r(.git)(/tree/...)(?q), github.com/o/r, or o/r."""
    cleaned = url.strip().split("?")[0].split("#")[0].rstrip("/")
    cleaned = re.sub(r"^(https?://)?(www\.)?github\.com/", "", cleaned, flags=re.I)
    parts = cleaned.split("/")
    if len(parts) < 2 or "://" in cleaned:
        raise HTTPException(400, "Invalid GitHub URL — expected https://github.com/owner/repo")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if repo in {".", ".."} or not (_OWNER_REPO[0].match(owner) and _OWNER_REPO[1].match(repo)):
        raise HTTPException(400, "Invalid GitHub URL — expected https://github.com/owner/repo")
    return owner, repo


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text.lower()
    )


async def gh_get(client: httpx.AsyncClient, path: str):
    try:
        resp = await client.get(f"{GITHUB_API}{path}", headers=_gh_headers())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not reach GitHub: {type(e).__name__}") from e
    if _is_rate_limited(resp):
        raise HTTPException(
            403,
            "GitHub rate limit exceeded. Set the GITHUB_TOKEN environment variable "
            "with a personal access token to get 5000 req/hour instead of 60."
        )
    if resp.status_code == 404:
        raise HTTPException(404, "Repository not found (or it is private).")
    if resp.status_code >= 400:
        raise HTTPException(502, f"GitHub API error {resp.status_code}")
    return resp.json()


async def get_repo(client: httpx.AsyncClient, owner: str, repo: str) -> tuple[dict, list[dict]]:
    """Fetch repo metadata + full recursive tree, cached to save rate limit."""
    key = (owner.lower(), repo.lower())
    hit = _repo_cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1], hit[2]
    meta = await gh_get(client, f"/repos/{owner}/{repo}")
    branch = quote(meta.get("default_branch") or "main", safe="")
    data = await gh_get(client, f"/repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    tree = data.get("tree", [])
    _repo_cache[key] = (time.monotonic(), meta, tree)
    return meta, tree


async def fetch_raw(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                    owner: str, repo: str, branch: str, path: str) -> str:
    url = f"{GITHUB_RAW}/{owner}/{repo}/{quote(branch, safe='')}/{quote(path)}"
    async with sem:
        resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def canonical_name(meta: dict, owner: str, repo: str) -> tuple[str, str]:
    """Renamed/transferred repos redirect on the API but not on raw.githubusercontent.com."""
    full = meta.get("full_name") or ""
    return tuple(full.split("/", 1)) if full.count("/") == 1 else (owner, repo)


_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*$|_tests?\.py$|(^|/)conftest\.py$", re.I)


def is_test_path(path: str) -> bool:
    """True for test dirs/files (tests/, test_x.py, x_test.py) — not names like latest-changes.yml."""
    return bool(_TEST_PATH.search(path))


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
    lower = path.lower()
    if name in ENTRY_NAMES:
        return "entry"
    if name in CRITICAL_NAMES:
        return "critical"
    if is_test_path(path):
        return "test"
    if any(k in lower for k in ("security", "auth", "oauth")):
        return "security"
    return ""


def select_impact_files(py_files: list[dict], target: str) -> list[str]:
    """Pick which Python files to download, nearest to the target first."""
    candidates = [f["path"] for f in py_files if f.get("size", 0) < MAX_PY_FILE_BYTES]
    target_top = target.split("/")[0]
    candidates.sort(key=lambda p: (p.split("/")[0] != target_top, p))
    chosen = candidates[:MAX_IMPACT_FILES]
    if target not in chosen:
        chosen = [target] + chosen[:MAX_IMPACT_FILES - 1]
    return chosen


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
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        meta, tree = await get_repo(client, owner, repo)
    owner, repo = canonical_name(meta, owner, repo)

    all_files = [f for f in tree if f["type"] == "blob"]
    py_paths  = [f["path"] for f in all_files if f["path"].endswith(".py")]
    dirs      = sorted({f["path"].rsplit("/", 1)[0] for f in all_files if "/" in f["path"]})

    structured = [
        {
            "path": item["path"],
            "type": "dir" if item["type"] == "tree" else "file",
            "size": item.get("size", 0),
            "tag": tag_file(item["path"]) if item["type"] == "blob" else "",
        }
        for item in tree[:TREE_DISPLAY_LIMIT]
    ]

    return {
        "owner": owner,
        "repo": repo,
        "description": meta.get("description") or "",
        "language": meta.get("language") or "",
        "stars": meta.get("stargazers_count", 0),
        "forks": meta.get("forks_count", 0),
        "total_files": len(all_files),
        "python_files": len(py_paths),
        "python_paths": py_paths,
        "top_dirs": dirs[:20],
        "tree": structured,
        "tree_total": len(tree),
    }


@app.post("/impact")
async def impact(req: ImpactRequest):
    owner, repo = parse_repo_url(req.repo_url)
    target = req.file_path.strip().lstrip("/")

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        meta, tree = await get_repo(client, owner, repo)
        branch = meta.get("default_branch") or "main"
        owner, repo = canonical_name(meta, owner, repo)
        py_files = [f for f in tree if f["type"] == "blob" and f["path"].endswith(".py")]
        if target not in {f["path"] for f in py_files}:
            raise HTTPException(404, f"Python file not found in repo: {target}")

        paths = select_impact_files(py_files, target)
        sem = asyncio.Semaphore(RAW_CONCURRENCY)
        results = await asyncio.gather(
            *[fetch_raw(client, sem, owner, repo, branch, p) for p in paths],
            return_exceptions=True,
        )

    contents = {p: r for p, r in zip(paths, results) if isinstance(r, str)}
    if not contents:
        raise HTTPException(502, "Could not download any Python files from GitHub.")

    forward, reverse = build_graphs([f["path"] for f in py_files], contents)
    affected = bfs(reverse, target)
    test_files = [f for f in affected if is_test_path(f)]

    scored = sorted(
        ({"path": p, "hops": h, "level": "high" if h == 1 else "medium" if h == 2 else "low"}
         for p, h in affected.items()),
        key=lambda x: (x["hops"], x["path"]),
    )

    return {
        "changed_file": target,
        "affected_count": len(affected),
        "test_count": len(test_files),
        "direct_imports": len(forward.get(target, ())),
        "direct_dependents": len(reverse.get(target, ())),
        "risk": risk_label(len(affected), len(contents)),
        "analyzed_files": len(contents),
        "total_python_files": len(py_files),
        "affected_files": scored[:40],
    }


@app.post("/guide")
async def guide(req: RepoRequest):
    owner, repo = parse_repo_url(req.repo_url)

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        meta, tree = await get_repo(client, owner, repo)
        owner, repo = canonical_name(meta, owner, repo)
        readme = ""
        try:
            data = await gh_get(client, f"/repos/{owner}/{repo}/readme")
            if data.get("encoding") == "base64":
                readme = base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
        except HTTPException as e:
            if e.status_code != 404:
                raise

    all_files  = [f for f in tree if f["type"] == "blob"]
    file_names = {f["path"] for f in all_files}
    py_paths   = [f["path"] for f in all_files if f["path"].endswith(".py")]
    top_dirs   = sorted({f["path"].split("/")[0] for f in all_files if "/" in f["path"]})

    # Detect setup commands
    setup = []
    if "pyproject.toml" in file_names or "setup.py" in file_names:
        setup.append('pip install -e ".[dev]"')
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

    entry_points = [p for p in py_paths if p.split("/")[-1] in ENTRY_NAMES]
    entry_points.sort(key=lambda p: (is_test_path(p), p.count("/"), p))

    has_tests = any(is_test_path(p) for p in py_paths)

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
