"""Enforce the application/CLI dependency direction across every import location."""
from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


def test_lower_modules_do_not_import_main_or_cli() -> None:
    root = Path(__file__).resolve().parents[1] / "digest"
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[0] == "cli" or relative.as_posix() in {"main.py", "__main__.py"}:
            continue
        package = ".".join(("digest", *relative.with_suffix("").parts[:-1]))
        # ast.walk includes function-local and TYPE_CHECKING imports.
        for node in ast.walk(ast.parse(path.read_text(), filename=str(relative))):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    module = resolve_name("." * node.level + module, package)
                targets = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            for target in targets:
                if target == "digest.main" or target == "digest.cli" or target.startswith("digest.cli."):
                    violations.append(f"{relative}:{node.lineno}: {target}")
    assert not violations, "Lower layers cannot depend on CLI owners:\n" + "\n".join(violations)
