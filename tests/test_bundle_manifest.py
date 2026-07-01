"""Bundle-manifest regression guard.

Ties the two ends of the frozen-app resource contract together so the packaged
``.exe`` can't silently ship without a file it reads at runtime — the original
silent-Windows-startup-crash class (the QSS theme was declared by the code but
never bundled), and the mirror problem where ``build.py`` *declares* a data
source that no longer exists on disk (the stale ``src/config/missions`` entry).

Two independent checks, both purely static (no PyInstaller / no build needed):

(a) Every literal-argument ``resource_path("a", "b", ...)`` call under ``src/``
    resolves to a path that EXISTS in the repo. Dynamic/variable-argument forms
    (e.g. ``resource_path(*_CATALOG_PARTS)``) are skipped — we can't statically
    know their target.

(b) Every static data source that ``build.py`` bundles (its ``--add-data``
    manifest) EXISTS in the repo. This is what catches a re-introduced stale
    ``src/config/missions``-style reference: build.py guards each entry with an
    ``.is_dir()``/``.is_file()`` check, so a bad path fails silently at build
    time — here it fails loudly instead.

Both checks resolve every path against the repo root, exactly the way
``src.core.resources.resource_path`` does in a dev checkout (``_MEIPASS`` mirrors
the same repo-relative layout in a frozen build), so a green test here means the
same relative paths will resolve inside the bundle.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Repo root = the directory that CONTAINS the ``src`` package (one level up from tests/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
_BUILD_PY = _REPO_ROOT / "build.py"


# ── shared AST helper ────────────────────────────────────────────────

def _root_relative_parts(node: ast.AST) -> list[str] | None:
    """Return the repo-relative string parts of a ``_ROOT / "a" / "b"`` chain.

    ``_ROOT`` alone -> ``[]`` (the repo root itself). Any chain not rooted at the
    ``_ROOT`` name (e.g. ``dist / _NAME``, an *output* path) or containing a
    non-string segment -> ``None`` (ignored). This keeps build.py's output-dir
    bookkeeping out of the manifest.
    """
    if isinstance(node, ast.Name):
        return [] if node.id == "_ROOT" else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _root_relative_parts(node.left)
        if left is None:
            return None
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return left + [right.value]
        return None
    return None


# ── (a) resource_path(...) targets the app reads ─────────────────────

def _iter_resource_path_targets() -> list[tuple[tuple[str, ...], Path, str]]:
    """Collect every literal-arg ``resource_path(...)`` call under ``src/``.

    Returns ``(parts, resolved_path, origin)`` tuples. Calls with any non-literal
    (variable / starred) argument are skipped.
    """
    found: list[tuple[tuple[str, ...], Path, str]] = []
    for py in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "resource_path"):
                continue
            if node.keywords:
                continue  # resource_path takes only *parts
            parts: list[str] = []
            literal = True
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    parts.append(arg.value)
                else:
                    literal = False  # Starred / Name / etc. -> dynamic, skip
                    break
            if not literal or not parts:
                continue
            resolved = _REPO_ROOT.joinpath(*parts)
            rel = py.relative_to(_REPO_ROOT).as_posix()
            origin = f"{rel}:{node.lineno}"
            found.append((tuple(parts), resolved, origin))
    return found


def test_resource_path_targets_exist():
    targets = _iter_resource_path_targets()
    # Sanity: we actually discovered the known call sites (guards against the AST
    # walk silently matching nothing after a refactor).
    assert targets, "no literal resource_path(...) calls found under src/ — parser broke?"

    # De-dupe by resolved path so the failure message lists each offender once.
    unique: dict[Path, list[str]] = {}
    for _parts, resolved, origin in targets:
        unique.setdefault(resolved, []).append(origin)

    missing = {
        path: origins for path, origins in unique.items() if not path.exists()
    }
    assert not missing, "resource_path() targets that do not exist on disk:\n" + "\n".join(
        f"  {path}  (read at {', '.join(sorted(callers))})"
        for path, callers in sorted(missing.items(), key=lambda kv: str(kv[0]))
    )


# ── (b) data sources build.py bundles ────────────────────────────────

def _bundled_sources() -> list[tuple[Path, str, bool]]:
    """Static list of the data *sources* build.py declares for the bundle.

    Tolerates the two shapes the manifest may take:
      1. imperative ``cmd.extend(["--add-data", f"{src}{sep}dest"])`` (current),
      2. a declarative ``DATA_FILES = [(src, dest), ...]`` / ``datas = [...]`` list.
    In both cases ``src`` is a ``_ROOT / ...`` path — resolved directly, via a
    ``name = _ROOT / ...`` assignment, or via the base of a ``dir.glob(...)`` loop.
    """
    tree = ast.parse(_BUILD_PY.read_text(encoding="utf-8"), filename=str(_BUILD_PY))

    # name -> repo-relative parts, for `name = _ROOT / "a" / "b" ...`
    var_parts: dict[str, list[str]] = {}
    # glob loop target -> the base variable it iterates (`for qss in theme_dir.glob(...)`)
    glob_base: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            parts = _root_relative_parts(node.value)
            if parts is not None:
                var_parts[node.targets[0].id] = parts
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            it = node.iter
            if (
                isinstance(it, ast.Call)
                and isinstance(it.func, ast.Attribute)
                and it.func.attr == "glob"
                and isinstance(it.func.value, ast.Name)
            ):
                glob_base[node.target.id] = it.func.value.id

    # Names tested with `.is_dir()/.exists()/.is_file()` in build.py — a source bundled behind such a
    # guard is OPTIONAL (a missing one is not a build failure, e.g. the deadmans-switch submodule on a
    # fresh clone), so the test must not require it to exist.
    guarded_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"is_dir", "exists", "is_file"}
            and isinstance(node.func.value, ast.Name)
        ):
            guarded_names.add(node.func.value.id)

    def _base_name(expr: ast.AST) -> str | None:
        """The leading Name id of a source expr (direct, or the FormattedValue of an f-string)."""
        if isinstance(expr, ast.JoinedStr):
            for piece in expr.values:
                if isinstance(piece, ast.FormattedValue):
                    return _base_name(piece.value)
            return None
        return expr.id if isinstance(expr, ast.Name) else None

    def _is_guarded(expr: ast.AST) -> bool:
        n = _base_name(expr)
        return n is not None and (n in guarded_names or glob_base.get(n, n) in guarded_names)

    def _parts_from_source_expr(expr: ast.AST) -> list[str] | None:
        """Resolve a --add-data / DATA_FILES source expression to repo-relative parts."""
        if isinstance(expr, ast.Name):
            name = glob_base.get(expr.id, expr.id)
            return var_parts.get(name)
        return _root_relative_parts(expr)

    def _parts_from_add_data_value(value: ast.AST) -> list[str] | None:
        """The source is the leading interpolation of ``f"{src}{sep}dest"``."""
        if not isinstance(value, ast.JoinedStr):
            return None
        for piece in value.values:
            if isinstance(piece, ast.FormattedValue):
                return _parts_from_source_expr(piece.value)
        return None

    sources: list[tuple[Path, str]] = []

    for node in ast.walk(tree):
        # Form 1: any list literal ["--add-data", <f-string source>, ...]
        if isinstance(node, ast.List):
            elts = node.elts
            for i, elt in enumerate(elts):
                if (
                    isinstance(elt, ast.Constant)
                    and elt.value == "--add-data"
                    and i + 1 < len(elts)
                ):
                    parts = _parts_from_add_data_value(elts[i + 1])
                    if parts is not None:
                        sources.append((_REPO_ROOT.joinpath(*parts), "--add-data", _is_guarded(elts[i + 1])))

        # Form 2: DATA_FILES / datas = [(src, dest), ...]
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"DATA_FILES", "datas", "DATAS", "_DATA_FILES"}
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            for entry in node.value.elts:
                if isinstance(entry, (ast.Tuple, ast.List)) and entry.elts:
                    parts = _parts_from_source_expr(entry.elts[0])
                    if parts is not None:
                        sources.append(
                            (_REPO_ROOT.joinpath(*parts), node.targets[0].id, _is_guarded(entry.elts[0]))
                        )

    return sources


def test_build_data_sources_exist():
    sources = _bundled_sources()
    assert sources, "no --add-data / DATA_FILES sources parsed from build.py — parser broke?"

    # A source is a failure only if build.py bundles it UNCONDITIONALLY (not behind an is_dir()/exists()
    # guard). Guarded-but-missing sources are expected — e.g. the optional deadmans-switch submodule on a
    # fresh clone (build.py skips them; CI checks out submodules recursively).
    missing = {path: label for path, label, guarded in sources if not guarded and not path.exists()}
    assert not missing, "build.py bundles (unconditionally) data sources that do not exist on disk:\n" + "\n".join(
        f"  {path}  (declared via {label})"
        for path, label in sorted(missing.items(), key=lambda kv: str(kv[0]))
    )
