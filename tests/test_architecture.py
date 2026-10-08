"""Guards the layering rule: `core` never depends on the delivery layers."""

import ast
from pathlib import Path

import cashflow

CORE = Path(cashflow.__file__).parent / "core"
FORBIDDEN = ("cashflow.api", "cashflow.mcp_server", "fastapi", "mcp", "starlette")


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_core_does_not_import_delivery_layers() -> None:
    violations = [
        f"{path.relative_to(CORE.parent)} imports {module}"
        for path in CORE.rglob("*.py")
        for module in _imported_modules(path)
        if any(module == f or module.startswith(f"{f}.") for f in FORBIDDEN)
    ]

    assert not violations, "\n".join(violations)
