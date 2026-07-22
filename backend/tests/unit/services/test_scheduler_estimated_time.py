"""Regression tests for the job-started estimated-time derivation.

Root cause (crashed twice in prod, 2026-07-21 dry-runs): the fallback read
``library_file.print_time_seconds`` — but ``LibraryFile`` has NO such column; the
estimate lives in its ``file_metadata`` JSON. Any dispatch whose archive carried no
time estimate hit that attribute and raised ``AttributeError``. The helper reads the
metadata instead (and tolerates a ``None`` ``file_metadata``).
"""

from __future__ import annotations

from types import SimpleNamespace

from backend.app.services.print_scheduler import _derive_estimated_time


def _archive(print_time_seconds):
    return SimpleNamespace(print_time_seconds=print_time_seconds)


def _library_file(file_metadata):
    # A LibraryFile stand-in: NO print_time_seconds attribute at all, so any code that
    # reads it (the old bug) raises AttributeError rather than silently passing.
    return SimpleNamespace(file_metadata=file_metadata)


def test_library_metadata_used_when_archive_has_no_estimate():
    # The exact shape that crashed: archive time is None, the estimate is in the
    # library file's file_metadata JSON.
    archive = _archive(None)
    library_file = _library_file({"print_time_seconds": 16735})
    assert _derive_estimated_time(archive, library_file) == 16735


def test_no_archive_reads_library_metadata():
    library_file = _library_file({"print_time_seconds": 4200})
    assert _derive_estimated_time(None, library_file) == 4200


def test_archive_estimate_preferred_over_library():
    archive = _archive(9000)
    library_file = _library_file({"print_time_seconds": 4200})
    assert _derive_estimated_time(archive, library_file) == 9000


def test_none_file_metadata_does_not_crash():
    # file_metadata itself may be None — must return None, never raise.
    assert _derive_estimated_time(None, _library_file(None)) is None


def test_metadata_without_estimate_returns_none():
    assert _derive_estimated_time(_archive(None), _library_file({"other": 1})) is None


def test_no_sources_returns_none():
    assert _derive_estimated_time(None, None) is None
