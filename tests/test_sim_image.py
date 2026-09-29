"""Static checks on the simulator image recipe (sim/Dockerfile, .dockerignore); no Docker needed."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "sim" / "Dockerfile"


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
