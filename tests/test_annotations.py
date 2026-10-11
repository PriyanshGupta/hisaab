"""Every variable gets a type annotation where Python allows one (owner's rule, 2026-10-11).

mypy --strict already refuses functions without annotated parameters and return
types. This covers what mypy does not: the first assignment of each name in a
module, class or function must be annotated (`total: int = 0`, not `total = 0`).
Later re-assignments stay plain, since annotating a name twice is an error.

Places Python has no syntax for an annotation are exempt: tuple unpacking,
for-loop and with-targets, comprehension variables, lambda parameters. So is the
body of an Enum, where an annotated name stops being a member.
"""

import ast
from pathlib import Path

from hisaab.config import PROJECT_ROOT

CHECKED: list[str] = ["src", "tests", "migrations", "spikes/slice0_oauth/authorize.py"]


def _files() -> list[Path]:
    found: list[Path] = []
    for entry in CHECKED:
        path: Path = PROJECT_ROOT / entry
        found += [path] if path.is_file() else sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return found


def _bound_by(node: ast.AST) -> set[str]:
    """Names a statement binds without an annotation being possible or needed."""
    names: set[str] = set()
    targets: list[ast.AST] = []
    if isinstance(node, (ast.For, ast.AsyncFor)):
        targets.append(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets += [item.optional_vars for item in node.items if item.optional_vars is not None]
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        names |= {(a.asname or a.name).split(".")[0] for a in node.names}
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        names |= set(node.names)
    elif isinstance(node, ast.Assign):
        targets += [t for t in node.targets if not isinstance(t, ast.Name)]  # unpacking
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        names.add(node.target.id)
    elif isinstance(node, ast.Try):
        names |= {h.name for h in node.handlers if h.name}
    for target in targets:
        names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return names


def _check_scope(body: list[ast.stmt], bound: set[str], where: str, problems: list[str]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            names: list[str] = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            for name in names:
                if name not in bound:
                    problems.append(f"{where}:{stmt.lineno} '{name}' has no type annotation")
                bound.add(name)
        for walrus in (n for n in ast.walk(stmt) if isinstance(n, ast.NamedExpr)):
            bound.add(walrus.target.id)  # no syntax for annotating these
        bound |= _bound_by(stmt)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params: list[ast.arg] = stmt.args.posonlyargs + stmt.args.args + stmt.args.kwonlyargs
            params += [a for a in (stmt.args.vararg, stmt.args.kwarg) if a]
            _check_scope(stmt.body, {a.arg for a in params}, where, problems)
        elif isinstance(stmt, ast.ClassDef):
            is_enum: bool = any(isinstance(b, (ast.Name, ast.Attribute)) and "Enum" in ast.unparse(b) for b in stmt.bases)
            if not is_enum:
                _check_scope(stmt.body, set(), where, problems)
        else:  # if / for / while / with / try / match blocks share the enclosing scope
            for field in ("body", "orelse", "finalbody"):
                _check_scope(getattr(stmt, field, []) or [], bound, where, problems)
            for handler in getattr(stmt, "handlers", []):
                _check_scope(handler.body, bound, where, problems)
            for case in getattr(stmt, "cases", []):
                _check_scope(case.body, bound, where, problems)


def test_every_variable_is_annotated_where_python_allows_it() -> None:
    problems: list[str] = []
    for path in _files():
        _check_scope(ast.parse(path.read_text()).body, set(), str(path.relative_to(PROJECT_ROOT)), problems)
    assert not problems, f"{len(problems)} unannotated variables:\n" + "\n".join(problems[:40])


def test_the_checker_catches_an_unannotated_variable_and_spares_what_python_cannot_annotate() -> None:
    source: str = (
        "import os\n"
        "LIMIT = 3\n"
        "def f(x: int) -> int:\n"
        "    total: int = 0\n"
        "    total = total + x\n"
        "    for i in range(x):\n"
        "        step = i\n"
        "    a, b = 1, 2\n"
        "    with open('f') as fh:\n"
        "        pass\n"
        "    if (n := x):\n"
        "        pass\n"
        "    return total\n"
        "class Colour(Enum):\n"
        "    RED = 'red'\n"
    )
    problems: list[str] = []
    _check_scope(ast.parse(source).body, set(), "m.py", problems)
    assert problems == ["m.py:2 'LIMIT' has no type annotation", "m.py:7 'step' has no type annotation"]
