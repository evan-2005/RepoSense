"""Python import graph construction and blast-radius traversal.

Imports are resolved in *path space* against the repo's own files, so both
package layouts (``pkg/__init__.py``), ``src/`` layouts and script-style
sibling imports work without guessing the source root.
"""
import ast
import posixpath
import sys
from collections import defaultdict, deque

STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | frozenset(sys.builtin_module_names)


def extract_imports(source: str) -> list[tuple[int, str, list[str]]]:
    """Return (level, module, names) for every import in a Python source string.

    ``import a.b``          -> (0, "a.b", [])
    ``from a.b import c``   -> (0, "a.b", ["c"])
    ``from ..x import y``   -> (2, "x", ["y"])
    ``from . import y``     -> (1, "", ["y"])
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((0, alias.name, []) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names if a.name != "*"]
            found.append((node.level or 0, node.module or "", names))
    return found


class ModuleResolver:
    """Maps dotted import names to file paths within a repository."""

    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self._by_suffix: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            parts = p.split("/")
            for i in range(len(parts)):
                self._by_suffix["/".join(parts[i:])].append(p)

    def _in_dir(self, directory: str, dotted: str) -> str | None:
        rel = dotted.replace(".", "/")
        base = posixpath.join(directory, rel) if directory else rel
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in self.paths:
                return cand
        return None

    def _by_name(self, importer: str, dotted: str) -> str | None:
        importer_dir = posixpath.dirname(importer)
        # Script-style import of a sibling module (sys.path[0] == script dir)
        sibling = self._in_dir(importer_dir, dotted)
        if sibling:
            return sibling
        is_single = "." not in dotted
        if is_single and dotted in STDLIB:
            return None
        rel = dotted.replace(".", "/")
        candidates = self._by_suffix.get(rel + ".py", []) + self._by_suffix.get(rel + "/__init__.py", [])
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Ambiguous: prefer the candidate sharing the longest directory prefix with the importer
        def shared(c: str) -> int:
            return len(posixpath.commonpath([importer_dir, posixpath.dirname(c)]) or "")
        ranked = sorted(candidates, key=lambda c: (-shared(c), len(c)))
        return None if is_single and shared(ranked[0]) == 0 else ranked[0]

    def resolve(self, importer: str, level: int, module: str, names: list[str]) -> set[str]:
        """Return the set of repo files referenced by one import statement."""
        targets: set[str] = set()
        if level:
            base = posixpath.dirname(importer)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            for name in names:  # `from .mod import sub` may import submodules
                hit = self._in_dir(base, f"{module}.{name}" if module else name)
                if hit:
                    targets.add(hit)
            # `from .mod import x` -> mod; `from . import x` (x not a module) -> package __init__
            if module or not targets:
                hit = self._in_dir(base, module or "__init__")
                if hit:
                    targets.add(hit)
            return targets

        for name in names:
            hit = self._by_name(importer, f"{module}.{name}")
            if hit:
                targets.add(hit)
        hit = self._by_name(importer, module)
        if hit:
            targets.add(hit)
        return targets


def build_graphs(paths: list[str], contents: dict[str, str]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (forward, reverse) graphs.

    forward[file] = files that ``file`` imports.
    reverse[file] = files that import ``file``.
    """
    resolver = ModuleResolver(paths)
    forward: dict[str, set[str]] = defaultdict(set)
    reverse: dict[str, set[str]] = defaultdict(set)
    for filepath, source in contents.items():
        for level, module, names in extract_imports(source):
            for target in resolver.resolve(filepath, level, module, names):
                if target != filepath:
                    forward[filepath].add(target)
                    reverse[target].add(filepath)
    return dict(forward), dict(reverse)


def bfs(reverse: dict[str, set[str]], start: str) -> dict[str, int]:
    """BFS through the reverse graph; returns {file: hop_distance} excluding start."""
    visited: dict[str, int] = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        cur = queue.popleft()
        for dep in sorted(reverse.get(cur, ())):
            if dep not in visited:
                visited[dep] = visited[cur] + 1
                queue.append(dep)
    visited.pop(start, None)
    return visited
