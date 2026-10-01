import pytest
from fastapi import HTTPException

from import_graph import build_graphs, bfs, extract_imports
from main import is_test_path, parse_repo_url, select_impact_files


@pytest.mark.parametrize("url", [
    "https://github.com/tiangolo/fastapi",
    "https://github.com/tiangolo/fastapi/",
    "https://github.com/tiangolo/fastapi.git",
    "https://www.github.com/tiangolo/fastapi/tree/master/fastapi",
    "github.com/tiangolo/fastapi?tab=readme",
    "tiangolo/fastapi",
])
def test_parse_repo_url_accepts_common_forms(url):
    assert parse_repo_url(url) == ("tiangolo", "fastapi")


@pytest.mark.parametrize("url", ["", "https://gitlab.com/a/b", "https://github.com/onlyowner", "a/../../x"])
def test_parse_repo_url_rejects_invalid(url):
    with pytest.raises(HTTPException):
        parse_repo_url(url)


def test_extract_imports_handles_relative_and_syntax_errors():
    src = "import os\nfrom . import routing\nfrom ..utils import helper\nfrom pkg.mod import *\n"
    assert extract_imports(src) == [
        (0, "os", []), (1, "", ["routing"]), (2, "utils", ["helper"]), (0, "pkg.mod", []),
    ]
    assert extract_imports("def (:") == []


def _graph(files: dict[str, str]):
    return build_graphs(list(files), files)


def test_package_relative_and_submodule_imports_resolve():
    files = {
        "pkg/__init__.py": "from .applications import App\n",
        "pkg/applications.py": "from pkg import routing\n",
        "pkg/routing.py": "from .utils import helper\n",
        "pkg/utils.py": "import os\n",
        "tests/test_app.py": "from pkg import App\n",
    }
    forward, reverse = _graph(files)
    assert forward["pkg/applications.py"] == {"pkg/routing.py", "pkg/__init__.py"}
    assert reverse["pkg/utils.py"] == {"pkg/routing.py"}
    affected = bfs(reverse, "pkg/utils.py")
    assert affected["pkg/routing.py"] == 1
    assert affected["tests/test_app.py"] == 4
    assert "pkg/utils.py" not in affected


def test_src_layout_and_sibling_script_imports():
    files = {
        "src/lib/core.py": "",
        "src/lib/api.py": "from lib.core import thing\n",
        "backend/main.py": "import helpers\n",
        "backend/helpers.py": "",
    }
    _, reverse = _graph(files)
    assert reverse["src/lib/core.py"] == {"src/lib/api.py"}
    assert reverse["backend/helpers.py"] == {"backend/main.py"}


def test_stdlib_names_do_not_match_unrelated_repo_files():
    files = {"pkg/types.py": "", "other/x.py": "import types\n"}
    _, reverse = _graph(files)
    assert "pkg/types.py" not in reverse


def test_duplicate_imports_count_once():
    files = {"a.py": "import b\nimport b\nfrom b import c\n", "b.py": ""}
    forward, _ = _graph(files)
    assert forward["a.py"] == {"b.py"}


def test_select_impact_files_always_includes_target():
    py = [{"path": f"aaa/m{i}.py", "size": 10} for i in range(1000)] + [{"path": "zzz/target.py", "size": 10}]
    chosen = select_impact_files(py, "zzz/target.py")
    assert "zzz/target.py" in chosen
    assert len(chosen) <= 400


@pytest.mark.parametrize("path,expected", [
    ("tests/test_app.py", True),
    ("pkg/testing/utils.py", True),
    ("test_main.py", True),
    ("pkg/routing_test.py", True),
    ("conftest.py", True),
    (".github/workflows/latest-changes.yml", False),
    ("pkg/contest.py", False),
    ("docs/attestation.md", False),
])
def test_is_test_path_matches_whole_names_only(path, expected):
    assert is_test_path(path) is expected
