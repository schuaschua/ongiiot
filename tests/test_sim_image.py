"""Static checks on the simulator image recipe (sim/Dockerfile, .dockerignore); no Docker needed."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "sim" / "Dockerfile"
UV_IMAGE = "ghcr.io/astral-sh/uv:0.12.19"


def instructions() -> list[tuple[str, str]]:
    """(KEYWORD, arguments) per instruction, with backslash continuations joined."""
    out: list[tuple[str, str]] = []
    pending = ""
    for line in DOCKERFILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        keyword, rest = (pending + line).split(None, 1)
        out.append((keyword.upper(), rest))
        pending = ""
    return out


def args(keyword: str) -> list[str]:
    return [a for k, a in instructions() if k == keyword]


def test_base_and_pinned_uv():
    froms = args("FROM")
    assert froms[-1] == "python:3.14-slim"
    uv_froms = [f for f in froms if "astral-sh/uv" in f]
    assert uv_froms and all(f.split()[0] == UV_IMAGE for f in uv_froms)
    for f in froms:
        image = f.split()[0]
        assert re.fullmatch(r"[\w./-]+:[\w.-]+", image) and not image.endswith(":latest"), image


def test_installs_runtime_deps_from_lock_only():
    runs = " ".join(args("RUN"))
    assert "uv sync" in runs
    assert "--locked" in runs and "--no-default-groups" in runs and "--no-install-project" in runs
    assert "--group" not in runs and "--all-groups" not in runs
    assert "pip install" not in runs


def test_copies_only_code_and_locks():
    copies = [a for a in args("COPY") if not a.startswith("--from=")]
    sources = {s for a in copies for s in a.split()[:-1]}
    assert sources == {"pyproject.toml", "uv.lock", "facility/", "sim/"}


def test_non_root_user_and_cmd():
    kw = [k for k, _ in instructions()]
    assert "USER" in kw
    (user,) = args("USER")
    assert user not in {"root", "0"}
    assert kw.index("USER") > max(i for i, k in enumerate(kw) if k == "RUN")
    (cmd,) = args("CMD")
    assert json.loads(cmd) == ["python", "-m", "sim.main"]


def test_dockerignore_excludes_workspace_noise():
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Allowlist: exclude everything, then re-include only what the image copies.
    assert lines[0] == "*"
    assert {"!pyproject.toml", "!uv.lock", "!facility", "!sim"} == {x for x in lines if x.startswith("!")}
    assert {".venv", ".git", "cdk.out", "_bmad*", ".work", "tests"} <= set(lines)
