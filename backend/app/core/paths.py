"""Shared path resolution helpers.

THE data-root authority. Every consumer of ``DATA_DIR`` goes through
``resolve_data_dir()``: ``auth.py`` (``.jwt_secret``), ``encryption.py``
(``.mfa_encryption_key``), ``api/routes/settings.py`` (key export/import) and
``core/config.py`` (``base_dir`` / ``archive_dir`` / ``database_url`` /
``plate_calibration_dir`` / ``erp_config_file``). Before this, ``config.py``
snapshotted its own ``os.environ["DATA_DIR"]`` read at import while this module
read it fresh — two authorities that answered differently the moment anything
moved the env var, which is exactly how the test harness ended up writing the
real repo-root ``bambuddy.db``.

Reading fresh — instead of caching at module import — is what lets a harness set
``DATA_DIR`` before importing the app and have every consumer agree.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_data_dir(*, legacy_fallback: Path | None = None) -> Path:
    """Return the data directory, reading ``DATA_DIR`` fresh from env on each call.

    ``DATA_DIR`` is set by every real deployment (``Dockerfile`` ENV,
    ``installers/windows/service/install-service.bat``, the systemd unit and the
    macOS plist written by ``install/install.sh``), so the fallback below is only
    ever reached by a developer running from a checkout.

    ``legacy_fallback`` exists for exactly one caller: ``config.py``'s
    ``base_dir`` / ``archive_dir`` / ``database_url``, whose historical
    DATA_DIR-unset location is the project root rather than ``<root>/data``.
    Passing it keeps that dev-only path bit-for-bit what it was; every other
    caller gets ``<project_root>/data``. Collapsing the two would relocate a
    developer's existing ``bambuddy.db`` and ``archive/``, so it is a deliberate
    follow-up, not a side effect of centralising the env read.
    """
    data_dir_env = os.environ.get("DATA_DIR")
    if data_dir_env:
        return Path(data_dir_env)
    if legacy_fallback is not None:
        return Path(legacy_fallback)
    return Path(__file__).parent.parent.parent.parent / "data"
