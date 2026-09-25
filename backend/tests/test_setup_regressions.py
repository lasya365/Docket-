"""Regression guards for bugs found while writing the setup guide.

Each of these once shipped broken. They are cheap, so they stay.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from docket.settings import load_settings


def _dotenv(path) -> dict[str, str]:
    """Parse .env.example exactly as `set -a; . ./.env` would, inline comments included."""
    if shutil.which("bash") is None:
        pytest.skip("no bash on this machine (Windows without Git Bash); "
                    "docket.ps1 parses .env itself and test_windows_has_every_command covers it")
    out = subprocess.run(
        ["bash", "-c", f"set -a; . '{path}'; env -0"], capture_output=True, text=True, check=True
    ).stdout
    keys = {line.split("=", 1)[0] for line in path.read_text().splitlines() if re.match(r"^[A-Z_]+=", line)}
    pairs = dict(item.split("=", 1) for item in out.split("\0") if "=" in item)
    return {k: pairs.get(k, "") for k in keys}


def test_every_env_example_value_loads_cleanly(repo_root):
    """An empty `DOCKET_CONFIG=` once crashed startup by being read as the path ""."""
    env = _dotenv(repo_root / ".env.example")
    assert env["DOCKET_CONFIG"] == "", "the example ships DOCKET_CONFIG empty; that must mean unset"
    s = load_settings(env=env)
    assert s.mode == "live"
    assert s.db_path == "./docket.sqlite"
    assert s.ingest_token == "change-me", "inline comments must not leak into values"


def test_env_example_documents_every_variable_docket_reads(repo_root):
    source = (repo_root / "backend" / "src" / "docket" / "settings.py").read_text()
    read = set(re.findall(r'"((?:DOCKET|GITHUB|FRESHSERVICE|ANTHROPIC)_[A-Z_]+)"', source))
    documented = set(re.findall(r"^([A-Z_]+)=", (repo_root / ".env.example").read_text(), re.M))
    assert read - documented == set(), f"undocumented: {sorted(read - documented)}"


def test_make_serve_loads_dotenv(repo_root):
    """`.env` was once created by the README and read by nothing, leaving ingest open."""
    makefile = (repo_root / "Makefile").read_text()
    serve = re.search(r"^serve:.*\n((?:\t.*\n)+)", makefile, re.M).group(1)
    assert "$(LOAD_ENV)" in serve
    assert ". ./.env" in makefile


def test_ui_sends_the_api_token_on_every_request(repo_root):
    """The UI once had no way to send DOCKET_API_TOKEN, so every write returned 401."""
    html = (repo_root / "frontend" / "index.html").read_text()
    api = re.search(r"function api\(path, opts\) \{.*?\n\}", html, re.S).group(0)
    assert "init.headers['Authorization'] = 'Bearer ' + tok" in api
    assert "r.status === 401" in api, "a 401 must tell the user what to do"
    assert "sessionStorage" in html and "localStorage.setItem('docket.apiToken'" not in html, \
        "the token lives for the tab only"
    assert 'id = \'apitoken\'' in html or "id = 'apitoken'" in html, "there must be a field to enter it"


def test_documented_start_commands_use_the_app_factory(repo_root):
    """`uvicorn docket.server:app` starts, then fails every request: `app` is None until get_app()."""
    for doc in ("README.md", "demo/README.md", "agent-setup/README.md"):
        text = (repo_root / doc).read_text()
        assert "docket.server:app " not in text and "docket.server:app\n" not in text, doc


def test_a_config_outside_the_repo_does_not_move_docket(tmp_path, repo_root):
    """DOCKET_CONFIG once re-rooted everything at the config's folder: the UI 404'd and
    demo/seed, the manifest and out/ were looked for in the wrong place."""
    import shutil

    elsewhere = tmp_path / "somewhere" / "config.json"
    elsewhere.parent.mkdir()
    shutil.copy(repo_root / "config.json", elsewhere)
    s = load_settings(env={"DOCKET_CONFIG": str(elsewhere)})
    assert s.config_path == elsewhere
    assert s.repo_root == repo_root
    assert (s.repo_root / "frontend" / "index.html").exists()
    assert s.path(s.config.manifest.path).exists(), "relative config paths resolve against the Docket install"


def test_windows_has_every_command_the_makefile_has(repo_root):
    """`make` is not a Windows command; docket.ps1 is the equivalent and must not drift."""
    import re

    make_verbs = set(re.findall(r"^([a-z-]+):", (repo_root / "Makefile").read_text(), re.M)) - {"help"}
    ps = (repo_root / "docket.ps1").read_text()
    ps_verbs = set(re.findall(r'^\s{4}"([a-z-]+)"', ps, re.M))
    assert make_verbs - ps_verbs == set(), f"missing on Windows: {sorted(make_verbs - ps_verbs)}"
    # the traps a Windows user hits first
    assert "Set-ExecutionPolicy" in ps, "must tell the user how to get past the execution policy"
    assert ".venv\\Scripts\\python.exe" in ps, "must use the Windows venv layout"
    assert "3.11" in ps, "must check the Python version like the Makefile does"


def test_readme_tells_windows_users_what_to_run(repo_root):
    readme = (repo_root / "README.md").read_text()
    assert "docket.ps1 install" in readme
    assert "Set-ExecutionPolicy" in readme
