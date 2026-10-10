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


def test_prepared_publication_has_no_raw_snapshot_or_orphan_entrypoints() -> None:
    from digest import edition_runtime
    from digest.application import preparation

    retired = {"finish_preparation", "_present_snapshot", "existing_preparation", "resume_preparation"}
    assert not retired.intersection(vars(edition_runtime))
    tree = ast.parse(Path(preparation.__file__).read_text())
    coordinator = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "_prepare_category_edition")
    calls = {node.func.id for node in ast.walk(coordinator)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert {"_accept_category_preparation", "present_preparation"} <= calls
    assert not calls.intersection(retired | {"save_preparation"})


def test_prepared_receipts_have_one_dispatch_application_owner() -> None:
    from digest import edition_runtime
    from digest.application import delivery, prepared_delivery
    from digest.delivery import edition

    assert "_merge_delivery" not in vars(edition_runtime)
    assert not {"PreparedOutcomePolicy", "_apply_prepared"}.intersection(vars(delivery))
    assert "mark_applied" not in vars(prepared_delivery) and "mark_applied" not in vars(edition)
    assert edition.send_prepared_edition is prepared_delivery.send_prepared_edition
    tree = ast.parse(Path(edition_runtime.__file__).read_text())
    coordinator = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "delivery_phase")
    calls = [node.func.id for node in ast.walk(coordinator)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert calls.count("send_prepared_edition") == 1
    assert not {"apply_confirmed_outcome", "mark_applied", "_merge_delivery"}.intersection(calls)
