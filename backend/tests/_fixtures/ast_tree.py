"""THE parsed view of ``backend/app`` for the structural (AST) tests.

Half a dozen tests each walk ``backend/app`` with ``rglob`` and ``ast.parse``
every file from scratch — ``unit/test_code_quality.py`` alone re-reads and
re-parses the whole tree five times across its three ownership tests, and
``unit/test_no_hardcoded_secrets.py``, ``unit/test_no_unsafe_path_joins.py``,
``unit/test_no_fail_open_in_auth.py`` and ``unit/services/test_bambu_mqtt.py``
do their own walks on top. Parsing 337 files takes ~0.79 s a pass and nothing
cached it.

This module reads and parses once per session and hands out the three shapes the
consumers actually use, because they genuinely differ:

* the **tree** (``test_code_quality``, ``test_bambu_mqtt``, ``test_no_hardcoded_secrets``)
* the **source lines** (``test_no_unsafe_path_joins`` and ``test_no_fail_open_in_auth``
  look for suppression markers on the offending line)
* the **path**, in three different bases — relative to ``backend/app``
  (``test_code_quality``'s owner tuples), relative to the repo root
  (``test_no_hardcoded_secrets``'s allowlist) and bare ``.name``.

Discovery mirrors the strictest consumer: ``__pycache__``/``.venv``/``venv`` are
excluded, order is sorted (one consumer's failure output depends on it), and a
file that will not read or parse is omitted from ``modules()`` and listed in
``unparsed`` instead of exploding the fixture.

Trees are SHARED. No consumer mutates one — the only ``ast.NodeVisitor`` in the
tree (``test_code_quality.DangerousImportVisitor``) keeps its state on the
visitor, not on the nodes — but a future visitor that rewrites nodes must copy
first.

Not every "AST-ish" test fits here, and they are left alone on purpose:
``unit/test_route_auth_coverage.py`` walks the live FastAPI ``app.routes`` and
parses nothing; ``unit/test_migrations_autonomy_posture.py`` greps source TEXT of
a non-recursive ``backend/app/services/*.py`` glob relative to the CWD; and
``test_spool_recovery``/``test_farm_policy``/``test_vp_mqtt_server`` parse a
single module or class via ``inspect.getsource``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

# backend/tests/_fixtures/ast_tree.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "backend" / "app"

_EXCLUDED_PARTS = frozenset({"__pycache__", ".venv", "venv"})


@dataclass(frozen=True)
class ParsedModule:
    """One module: absolute path, its text, and its parsed tree."""

    path: Path
    source: str
    tree: ast.Module
    root: Path
    repo_root: Path

    @property
    def lines(self) -> list[str]:
        """``source.splitlines()`` — 1-based line N is ``lines[N - 1]``."""
        return self.source.splitlines()

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def rel_to_root(self) -> Path:
        """Relative to the scanned root (``backend/app`` for ``app_sources``)."""
        return self.path.relative_to(self.root)

    @property
    def rel_to_repo(self) -> Path:
        return self.path.relative_to(self.repo_root)

    @property
    def posix_rel_to_repo(self) -> str:
        """``backend/tests/unit/foo.py`` — the form the ownership allowlists use."""
        return self.rel_to_repo.as_posix()


class ParsedTree:
    """Every parsed module under ``root``, read and parsed once.

    Used for ``backend/app`` (the structural tests) and for ``backend/tests``
    itself (``unit/test_fixture_ownership.py``), so there is one walker rather
    than one per caller.
    """

    def __init__(self, root: Path = APP_ROOT, repo_root: Path = REPO_ROOT) -> None:
        self.root = root
        self.repo_root = repo_root
        self._modules: dict[Path, ParsedModule] = {}
        self.unparsed: tuple[Path, ...] = ()

        unparsed: list[Path] = []
        for path in sorted(root.rglob("*.py")):
            if _EXCLUDED_PARTS & set(path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                unparsed.append(path)
                continue
            self._modules[path] = ParsedModule(
                path=path, source=source, tree=tree, root=root, repo_root=repo_root
            )
        self.unparsed = tuple(unparsed)

    def modules(
        self,
        *,
        under: str | Path | Sequence[str | Path] | None = None,
        exclude_names: Iterable[str] = (),
        skip_init: bool = False,
    ) -> tuple[ParsedModule, ...]:
        """The modules to scan, in sorted path order.

        ``under`` narrows to one or more subtrees, given relative to
        ``backend/app`` (``"services"``, ``("api/routes", "services")``) or as an
        absolute path. ``exclude_names`` drops files by basename; ``skip_init``
        drops ``__init__.py``.
        """
        roots: list[Path]
        if under is None:
            roots = [self.root]
        else:
            candidates = [under] if isinstance(under, str | Path) else list(under)
            roots = [c if Path(c).is_absolute() else self.root / c for c in map(Path, candidates)]

        excluded = set(exclude_names)
        selected: list[ParsedModule] = []
        for path, module in self._modules.items():
            if not any(path.is_relative_to(r) for r in roots):
                continue
            if path.name in excluded:
                continue
            if skip_init and path.name == "__init__.py":
                continue
            selected.append(module)
        return tuple(sorted(selected, key=lambda m: m.path))

    def get(self, path: str | Path) -> ParsedModule:
        """One module by path — absolute, or relative to ``backend/app``."""
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = self.root / resolved
        try:
            return self._modules[resolved]
        except KeyError:
            raise AssertionError(f"{resolved} is not a parsed module under {self.root}") from None


@pytest.fixture(scope="session")
def app_sources() -> ParsedTree:
    """Every ``backend/app`` module, read and parsed once for the whole session.

    ``ParsedTree`` itself is reusable with another root —
    ``unit/test_fixture_ownership.py`` points it at ``backend/tests`` — so there
    is one walker in the tree, not one per caller.
    """
    return ParsedTree()
