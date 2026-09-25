"""RFC 15.3: plane boundaries are enforced by a test, not by good intentions (P12).

decide/ may import the standard library, `pathspec` (RFC 9.0 requires it for glob
matching) and `docket.models.*`. Nothing else.

collectors/ and correlate/ may not import docket.record. Those packages are written
in parallel with this one, so the test passes over whatever files exist today.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ALLOWED_THIRD_PARTY = {"pathspec"}
STDLIB = set(sys.stdlib_module_names)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _imported_roots(path: Path) -> list[tuple[str, int]]:
    """Every module root this file imports, with the line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:            # relative import: stays inside the package
                continue
            if node.module:
                found.append((node.module, node.lineno))
    return found


def _src(repo_root: Path) -> Path:
    return repo_root / "backend" / "src" / "docket"


def test_decide_imports_only_stdlib_models_and_pathspec(repo_root: Path) -> None:
    decide = _src(repo_root) / "decide"
    files = _python_files(decide)
    assert files, "decide/ has no python files, which cannot be right"

    offences: list[str] = []
    for path in files:
        for module, lineno in _imported_roots(path):
            root = module.split(".")[0]
            if root in STDLIB or root in ALLOWED_THIRD_PARTY:
                continue
            if module == "docket.models" or module.startswith("docket.models."):
                continue
            if module == "docket.decide" or module.startswith("docket.decide."):
                continue
            offences.append(f"{path.relative_to(repo_root)}:{lineno} imports {module}")
    assert offences == [], "decide/ imported something it may not:\n" + "\n".join(offences)


def test_decide_never_imports_the_other_planes(repo_root: Path) -> None:
    banned = ("docket.collectors", "docket.correlate", "docket.record", "docket.settings",
              "docket.pipeline", "docket.server", "docket.mcp_server")
    offences: list[str] = []
    for path in _python_files(_src(repo_root) / "decide"):
        for module, lineno in _imported_roots(path):
            if any(module == b or module.startswith(b + ".") for b in banned):
                offences.append(f"{path.relative_to(repo_root)}:{lineno} imports {module}")
    assert offences == []


def test_decide_reads_no_clock_and_no_files(repo_root: Path) -> None:
    """No datetime.now(), no open(), no random anywhere under decide/ (RFC 0 rule 3)."""
    banned_calls = {"now", "utcnow", "today", "open", "urlopen"}
    banned_modules = {"random", "secrets", "time", "socket", "subprocess", "os"}
    offences: list[str] = []
    for path in _python_files(_src(repo_root) / "decide"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else ""
                )
                if name in banned_calls:
                    offences.append(f"{path.relative_to(repo_root)}:{node.lineno} calls {name}()")
        for module, lineno in _imported_roots(path):
            if module.split(".")[0] in banned_modules:
                offences.append(f"{path.relative_to(repo_root)}:{lineno} imports {module}")
    assert offences == [], "decide/ must stay pure:\n" + "\n".join(offences)


@pytest.mark.parametrize("package", ["collectors", "correlate"])
def test_planes_1_and_2_never_import_plane_4(repo_root: Path, package: str) -> None:
    root = _src(repo_root) / package
    if not root.exists():
        pytest.skip(f"{package}/ does not exist yet")
    offences: list[str] = []
    for path in _python_files(root):
        for module, lineno in _imported_roots(path):
            if module == "docket.record" or module.startswith("docket.record."):
                offences.append(f"{path.relative_to(repo_root)}:{lineno} imports {module}")
    assert offences == [], f"{package}/ must not write anywhere:\n" + "\n".join(offences)
