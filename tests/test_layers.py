"""Layer rules (spine, Design Paradigm): checked on the import statements of every module.

- ``detect/core/`` imports only the standard library and ``detect.core`` itself (types from
  ``detect/core/models.py``); never adapters, the handler, ``facility``, boto3 or PyYAML.
- ``sim/``, ``detect/`` and ``ops/`` never import each other.
- ``detect/adapters/`` imports no ``facility`` (the handler maps the model into core types).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("sim", "detect", "ops")


def imports(path: Path) -> set[str]:
    return parse_imports(path.read_text(), path.relative_to(ROOT).with_suffix("").parts)


def parse_imports(source: str, module_parts: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = ".".join(module_parts[: -node.level])
                out.add(f"{pkg}.{node.module}" if node.module else pkg)
            else:
                out.add(node.module)
    return out


def modules(package: str) -> list[Path]:
    return sorted((ROOT / package).rglob("*.py"))


def top(name: str) -> str:
    return name.split(".")[0]


def test_core_imports_only_stdlib_and_core():
    for path in modules("detect/core"):
        for name in imports(path):
            if top(name) == "detect":
                assert name == "detect.core" or name.startswith("detect.core."), (str(path.relative_to(ROOT)), name)
            else:
                assert top(name) in sys.stdlib_module_names or top(name) == "__future__", (str(path.relative_to(ROOT)), name)


def test_adapters_do_not_import_facility_or_handler():
    for path in modules("detect/adapters"):
        for name in imports(path):
            assert top(name) != "facility", (str(path.relative_to(ROOT)), name)
            assert not name.startswith("detect.handler"), (str(path.relative_to(ROOT)), name)


def test_sim_detect_ops_never_import_each_other():
    for package in PACKAGES:
        for path in modules(package):
            for name in imports(path):
                assert top(name) not in set(PACKAGES) - {package}, (str(path.relative_to(ROOT)), name)


def test_checker_sees_absolute_and_relative_imports():
    found = parse_imports("from ..adapters import reader\nimport boto3\nfrom . import models\n", ("detect", "core", "x"))
    assert found == {"detect.adapters", "boto3", "detect.core"}
