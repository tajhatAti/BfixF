"""Static check: catch `NameError` before it reaches a running bot.

Written after the deployed bot crashed with `NameError: name 'scheduler' is not
defined` — `bot.py` called `scheduler.start_all()` without importing it. That is
exactly the kind of mistake `ast.parse`/`import app.handlers.x` never notices,
because the broken line only runs at startup.

Run it by hand:  python tools/static_check.py app web tools tests bot.py run.py
It also runs as part of the test-suite (`tests/test_static_check.py`).
"""
from __future__ import annotations
import builtins, pathlib, symtable, sys

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__",
                                 "__spec__", "__loader__", "__builtins__", "__debug__",
                                 "WindowsError", "reveal_locals", "reveal_type"}

def undefined_names(source: str, filename: str) -> list[tuple[str, int]]:
    top = symtable.symtable(source, filename, "exec")
    defined = {s.get_name() for s in top.get_symbols()
               if s.is_assigned() or s.is_imported() or s.is_namespace()
               or s.is_parameter() or s.is_free()}
    problems: list[tuple[str, int]] = []
    seen: set[str] = set()

    def walk(table, is_module: bool = False):
        for sym in table.get_symbols():
            name = sym.get_name()
            if is_module:
                if sym.is_referenced() and not (sym.is_assigned() or sym.is_imported()
                                                or sym.is_namespace() or sym.is_parameter()):
                    if name not in BUILTINS and name not in defined:
                        problems.append((name, table.get_lineno()))
            elif sym.is_global() and not (sym.is_assigned() or sym.is_imported()
                                          or sym.is_namespace() or sym.is_parameter()):
                if name not in BUILTINS and name not in defined:
                    problems.append((name, table.get_lineno()))
        for child in table.get_children():
            walk(child)

    walk(top, is_module=True)
    # dedupe on name only (one report per missing name)
    out = []
    for name, lineno in problems:
        if name in seen:
            continue
        seen.add(name)
        out.append((name, lineno))
    return out


def iter_python_files(roots: list[str]):
    for root in roots:
        path = pathlib.Path(root)
        candidates = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for candidate in candidates:
            if candidate.suffix == ".py" and "__pycache__" not in candidate.parts:
                yield candidate


def check_paths(roots: list[str]) -> int:
    bad = 0
    for path in iter_python_files(roots):
        if True:
            if True:
                source = path.read_text(encoding="utf-8")
            try:
                problems = undefined_names(source, str(path))
            except SyntaxError as exc:
                print(f"{path}: syntax error {exc}")
                bad += 1
                continue
            for name, lineno in problems:
                print(f"{path}:{lineno}: undefined name '{name}'")
                bad += 1
    return bad


if __name__ == "__main__":
    sys.exit(1 if check_paths(sys.argv[1:] or ["app", "web", "tools", "tests"]) else 0)
