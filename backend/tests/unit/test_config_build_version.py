"""Unit tests for the installer build-stamp resolution
(``config._resolve_build_version``).

The Windows installer stages a ``VERSION`` file next to the app tree carrying
the full build string (``APP_VERSION+<UTCts>.<sha>``). The backend reports it
at runtime so an operator can verify which build is deployed — but a stale or
foreign file must never be trusted, and the resolver must never raise.
"""

from __future__ import annotations

import pytest

from backend.app.core.config import _resolve_build_version

APP_VERSION = "0.2.4.8"


@pytest.mark.unit
def test_valid_prefixed_file_is_stamped(tmp_path):
    """A VERSION file whose content starts with APP_VERSION is authoritative."""
    stamp = f"{APP_VERSION}+19990101000000.deadbeef"
    version_file = tmp_path / "VERSION"
    version_file.write_text(stamp)

    assert _resolve_build_version(version_file, APP_VERSION) == stamp


@pytest.mark.unit
def test_valid_file_is_whitespace_stripped(tmp_path):
    """Surrounding whitespace/newlines are stripped."""
    stamp = f"{APP_VERSION}+19990101000000.deadbeef"
    version_file = tmp_path / "VERSION"
    version_file.write_text(f"  {stamp}\n")

    assert _resolve_build_version(version_file, APP_VERSION) == stamp


@pytest.mark.unit
def test_absent_file_falls_back_to_app_version(tmp_path):
    """No file (dev checkout) → plain APP_VERSION, no exception."""
    version_file = tmp_path / "VERSION"  # never created
    assert _resolve_build_version(version_file, APP_VERSION) == APP_VERSION


@pytest.mark.unit
def test_mismatched_prefix_falls_back_to_app_version(tmp_path):
    """A stale/foreign file that doesn't start with APP_VERSION must not lie."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("9.9.9+deadbeef")

    assert _resolve_build_version(version_file, APP_VERSION) == APP_VERSION


@pytest.mark.unit
def test_empty_file_falls_back_to_app_version(tmp_path):
    """An empty file is not a valid stamp."""
    version_file = tmp_path / "VERSION"
    version_file.write_text("")

    assert _resolve_build_version(version_file, APP_VERSION) == APP_VERSION
