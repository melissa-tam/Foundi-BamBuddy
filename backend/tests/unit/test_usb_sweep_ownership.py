"""One owner per fact of the USB sweep (2026-10-05). SOURCE pins, in the style of
``test_code_quality.py``: every failure they catch is a well-formed second copy that every
behaviour test passes.

* WHO deletes from a printer's storage — a short, reasoned list. A second background deleter is
  how a durable file disappears (memory: the durable-file deletion invariant), and the deletes
  this list replaced each judged "is it mine?" on their own: the sweep by name variants, the
  refusal paths by a ``bool`` of an enum that is always truthy.
* WHAT must stay — ``job_terminal.live_upload_paths``, read by the sweep, never re-derived.
* WHAT an eject file is called — ``eject.remote.expected_eject_stem``; the three inline copies
  of the rule it replaced are how the start deadline would have named a file the dispatcher
  never wrote.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent / "app"


def _app_files() -> list[Path]:
    return sorted(path for path in APP_DIR.rglob("*.py") if "__pycache__" not in path.parts)


def _relative(path: Path) -> str:
    return path.relative_to(APP_DIR).as_posix()


def _scoped(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every node with the qualified name of the def/class it sits in (``<module>`` at top level)."""
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, (*stack, child.name))
            else:
                out.append((".".join(stack) or "<module>", child))
                walk(child, stack)

    walk(tree, ())
    return out


# --------------------------------------------------------------------------- #
# (a) Who deletes from a printer's storage
# --------------------------------------------------------------------------- #
_DELETE_CALLS = frozenset({"delete_file_async", "delete_file"})

# Every caller of an FTPS delete, and why it may delete.
_FTP_DELETERS: dict[tuple[str, str], str] = {
    ("api/routes/printers.py", "delete_printer_file"): "the operator's file-manager delete",
    ("services/job_terminal.py", "delete_uploaded_file"): "an ended job's own upload (#374 / #1542)",
    ("services/usb_storage.py", "_sweep_drive"): "THE USB sweep: old recordings and farm-written root files",
    ("services/bambu_ftp.py", "remove_abandoned_upload"): "THE helper every abandonment exit removes its upload with",
    ("services/bambu_ftp.py", "delete_file_async._delete"): "the delete wrapper's own session body",
    ("services/bambu_ftp.py", "BambuFTPClient.upload_file"): "a cancelled upload's own partial file",
    ("services/print_scheduler.py", "PrintScheduler._start_print"): (
        "the pre-upload clear of the path this dispatch is about to write (a STOR over an existing file 553s)"
    ),
}


def _ftp_delete_callers() -> dict[tuple[str, str], list[int]]:
    found: dict[tuple[str, str], list[int]] = {}
    for path in _app_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qual, node in _scoped(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _DELETE_CALLS:
                found.setdefault((_relative(path), qual), []).append(node.lineno)
    return found


class TestUsbSweepOwnership:
    def test_only_the_reasoned_lanes_delete_from_a_printer(self):
        strays = [
            f"  - {where}:{lines} ({qual})"
            for (where, qual), lines in sorted(_ftp_delete_callers().items())
            if (where, qual) not in _FTP_DELETERS
        ]
        assert not strays, (
            "An FTPS delete is called outside the reasoned lanes:\n"
            + "\n".join(strays)
            + "\n\nA lane removing its OWN abandoned upload calls bambu_ftp.remove_abandoned_upload; "
            "anything that clears a drive belongs to usb_storage's sweep. A genuinely new deleter "
            "goes on _FTP_DELETERS with its reason."
        )

    def test_every_reasoned_deleter_still_deletes(self):
        """The liveness half: an allowlist entry whose caller is gone would excuse the next one."""
        stale = sorted(set(_FTP_DELETERS) - set(_ftp_delete_callers()))
        assert not stale, f"allowlisted FTPS deleters that no longer delete: {stale}"

    # ----------------------------------------------------------------------- #
    # (b) The keep-set is job_terminal's
    # ----------------------------------------------------------------------- #
    def test_the_sweep_reads_its_keep_set_from_job_terminal(self):
        tree = ast.parse((APP_DIR / "services" / "usb_storage.py").read_text(encoding="utf-8"))
        # A MODULE-level import: the call-time one existed only to dodge the cycle the upload
        # registry closed (usb_storage -> job_terminal -> ... -> eject.remote -> usb_storage).
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "backend.app.services.job_terminal"
            and any(alias.name == "live_upload_paths" for alias in node.names)
            for node in tree.body
        )
        called = any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "live_upload_paths"
            for node in ast.walk(tree)
        )
        assert imported and called, "usb_storage must take its keep-set from job_terminal.live_upload_paths"

    def test_the_sweep_holds_no_keep_set_of_its_own(self):
        tree = ast.parse((APP_DIR / "services" / "usb_storage.py").read_text(encoding="utf-8"))
        defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not defined & {"_in_use_remote_names", "_name_variants"}
        # Which unit prints which file is the keep-set owner's question, never the sweep's.
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert "PrintQueueItem" not in names

    # ----------------------------------------------------------------------- #
    # (d) ONE upload registry, in a leaf module
    # ----------------------------------------------------------------------- #
    def test_the_upload_registry_has_one_owner(self):
        """``upload_in_flight`` and its set are defined in ``usb_uploads`` alone, and nothing but
        that context manager writes the set."""
        owners: list[str] = []
        writers: list[str] = []
        for path in _app_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            owners += [
                _relative(path)
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upload_in_flight"
            ]
            for qual, node in _scoped(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"add", "discard", "clear"}
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "_uploads_in_flight"
                ):
                    writers.append(f"{_relative(path)} ({qual})")
        assert owners == ["services/usb_uploads.py"], owners
        assert sorted(writers) == [
            "services/usb_uploads.py (_reset_state)",
            "services/usb_uploads.py (upload_in_flight)",
            "services/usb_uploads.py (upload_in_flight)",
        ], writers

    def test_the_upload_registry_is_a_leaf(self):
        """Every upload lane imports it; if it imported a farm service back, the cycle it was
        extracted to break would return."""
        tree = ast.parse((APP_DIR / "services" / "usb_uploads.py").read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)] + [
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ]
        assert not [name for name in imports if name.startswith("backend.")], imports

    def test_the_sweep_names_eject_files_through_the_eject_lane_at_module_level(self):
        tree = ast.parse((APP_DIR / "services" / "usb_storage.py").read_text(encoding="utf-8"))
        assert any(
            isinstance(node, ast.ImportFrom)
            and node.module == "backend.app.services.eject.remote"
            and any(alias.name == "is_eject_job_name" for alias in node.names)
            for node in tree.body
        )

    # ----------------------------------------------------------------------- #
    # (c) ONE eject-stem builder
    # ----------------------------------------------------------------------- #
    def test_the_eject_stem_has_one_builder(self):
        builders: list[str] = []
        strays: list[str] = []
        for path in _app_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for qual, node in _scoped(tree):
                if not isinstance(node, ast.JoinedStr) or not node.values:
                    continue
                head = node.values[0]
                if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                    continue
                if not head.value.lower().startswith("eject_"):
                    continue
                where = f"{_relative(path)}:{node.lineno} ({qual})"
                if (_relative(path), qual) == ("services/eject/remote.py", "expected_eject_stem"):
                    builders.append(where)
                else:
                    strays.append(where)
        assert not strays, (
            "An eject job stem is spelled outside eject.remote.expected_eject_stem:\n  "
            + "\n  ".join(strays)
            + "\nAsk expected_eject_stem(pending, printer_id) — the dispatch, the matcher and the start "
            "deadline must name the same file."
        )
        # Liveness: the builder still mints both shapes (unit-bound and manual).
        assert len(builders) == 2, builders
