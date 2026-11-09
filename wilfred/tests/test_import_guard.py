"""CI guard: hyperwilfred-core must NEVER import from common_py.factory.*

The factory package's ``__init__`` eagerly imports the entire dropped Linear/Symphony spine,
so a single ``from common_py.factory.x import y`` drags all the baggage back in. The keepers
are FORKED as standalone source — this test fails the build if anyone re-introduces the
import trap. Docstring mentions of ``common_py`` (provenance notes) are allowed; only real
import statements are forbidden.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "hyperwilfred_core"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_no_common_py_factory_imports() -> None:
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "common_py" or node.module.startswith("common_py."):
                    offenders.append(f"{path.name}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "common_py" or alias.name.startswith("common_py."):
                        offenders.append(f"{path.name}: import {alias.name}")
    assert offenders == [], "forbidden common_py imports found:\n" + "\n".join(offenders)


def test_engine_never_imports_an_llm_or_agent_sdk() -> None:
    """Defense of the crucial invariant: the engine calls no LLM and spawns no sub-agent."""
    engine_src = (SRC / "engine.py").read_text(encoding="utf-8")
    for token in ("import anthropic", "import openai", "from anthropic", "from openai"):
        assert token not in engine_src, f"engine.py must not reference {token!r}"
