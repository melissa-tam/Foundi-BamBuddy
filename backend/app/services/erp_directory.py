"""ERP directory authentication service (Foundi print farm).

Two operators authenticate into the farm app with their ERP credentials
(roles ADMIN | EDITOR | VIEWER) held in the first-party MariaDB identity
store ``Foundi_management_system.users``. This service is the single owner
of the ERP-login logic; the ``routes/auth.py`` hook that calls it stays
small and mirrors the existing LDAP seam.

Design (user-approved trade-offs):
- One parameterized ``SELECT`` per login run, executed off the event loop in
  a worker thread (``asyncio.to_thread``) because PyMySQL is blocking. The
  bcrypt verification runs in the same thread for the same reason.
- Tri-state result so the caller can distinguish a definitive "no" from a
  transient outage:
    OK          -> credentials valid AND account usable; caller provisions /
                   syncs a local mirror user and mirrors the bcrypt hash.
    REJECTED    -> row missing, wrong password, inactive, or locked. Final —
                   the caller must deny outright and must NOT fall back to a
                   cached credential (a stale hash must never authenticate an
                   ERP-managed user while the ERP is reachable and says no).
    UNREACHABLE -> connection / timeout / operational error. The caller may
                   fall back to the cached (mirrored) credential for an
                   existing ERP-managed user only.

ERP password hashes are bcrypt ``$2a$`` — verified via the app's canonical
bcrypt verifier ``backend.app.core.auth.verify_bcrypt_password`` (the bcrypt
library directly, because this environment's passlib bcrypt backend is broken
against bcrypt >= 4). The same helper backs the hash-agnostic
``verify_password`` dispatch, so there is exactly one bcrypt implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from backend.app.core.auth import verify_bcrypt_password

logger = logging.getLogger(__name__)

# Seeded default mapping (ERP role -> Bambuddy system group). Applied when the
# operator enables ERP login without customizing ``erp_role_group_mapping``.
DEFAULT_ROLE_GROUP_MAPPING: dict[str, str] = {
    "ADMIN": "Administrators",
    "EDITOR": "Operators",
    "VIEWER": "Viewers",
}

# The ERP connection settings this service reads. These live in TWO layered
# sources resolved by ``resolve_erp_settings``: a deploy-provided env file
# (the default, so a fresh install works with zero configuration) and the
# app's own ``settings`` KV table (a per-instance override). No enable flag —
# presence of host/name/user is the switch (see ``parse_erp_config``).
ERP_SETTING_KEYS: tuple[str, ...] = (
    "erp_db_host",
    "erp_db_port",
    "erp_db_name",
    "erp_db_user",
    "erp_db_password",
    "erp_db_ssl",
    "erp_role_group_mapping",
)


class ErpAuthStatus(str, Enum):
    """Tri-state outcome of an ERP login attempt."""

    OK = "ok"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"


@dataclass
class ErpConfig:
    """ERP directory connection config parsed from settings KV."""

    host: str
    port: int
    database: str
    user: str
    password: str
    ssl: bool
    role_group_mapping: dict[str, str]


@dataclass
class ErpUser:
    """An authenticated ERP user (username / role / active) plus the fetched
    bcrypt hash so the caller can mirror it into the local cache without
    recomputation."""

    username: str
    role: str
    active: bool
    password_hash: str


@dataclass
class ErpAuthResult:
    """Tri-state result. ``erp_user`` is populated only for OK."""

    status: ErpAuthStatus
    erp_user: ErpUser | None = None


def parse_erp_config(settings: dict[str, str]) -> ErpConfig | None:
    """Parse ERP config from settings key-value pairs.

    ERP login is a standing auth method — there is no enable flag. It is
    active whenever the mandatory connection fields (host / database / user)
    are all present; otherwise this returns None and the caller skips ERP.
    That "presence => active" gate is the sole switch: clearing the
    connection config (removing ``erp.env`` or blanking the DB override) is
    how an operator turns ERP login off.
    """
    host = settings.get("erp_db_host", "").strip()
    database = settings.get("erp_db_name", "").strip()
    user = settings.get("erp_db_user", "").strip()
    if not (host and database and user):
        return None

    try:
        port = int(settings.get("erp_db_port", "3306") or "3306")
    except (TypeError, ValueError):
        port = 3306

    raw = settings.get("erp_role_group_mapping", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            mapping = parsed if isinstance(parsed, dict) else DEFAULT_ROLE_GROUP_MAPPING
        except json.JSONDecodeError:
            mapping = DEFAULT_ROLE_GROUP_MAPPING
    else:
        # Empty/absent -> apply the seeded default so a minimal config still
        # maps ERP roles onto farm groups.
        mapping = DEFAULT_ROLE_GROUP_MAPPING

    return ErpConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=settings.get("erp_db_password", ""),
        ssl=settings.get("erp_db_ssl", "false").lower() == "true",
        role_group_mapping=mapping,
    )


def load_erp_env_defaults(path) -> dict[str, str]:
    """Read the deploy-provided ERP connection file (``erp.env``) into a dict
    of ``ERP_SETTING_KEYS``.

    The file is delivered by the installer to a restricted-ACL config dir so a
    fresh instance has ERP login working with no post-install configuration.
    Parsing reuses ``python-dotenv`` (a pinned dependency) — it does NOT mutate
    ``os.environ``. Any missing/unreadable/malformed file yields ``{}`` (fail to
    "no env config", never crash startup); only recognised keys are returned.
    """
    if not path:
        return {}
    try:
        from pathlib import Path

        from dotenv import dotenv_values

        if not Path(path).is_file():
            return {}
        values = dotenv_values(str(path))
        return {k: v for k, v in values.items() if k in ERP_SETTING_KEYS and v is not None}
    except Exception as exc:  # unreadable / malformed -> behave as "no env config"
        logger.warning("Could not load ERP env config from %s: %s", path, exc)
        return {}


async def resolve_erp_settings(db) -> dict[str, str]:
    """Single owner of ERP-config resolution: layer the deploy env-file
    defaults UNDER the app's ``settings`` KV overrides and return the merged
    dict for ``parse_erp_config``.

    Both callers use this — the login hook (``routes/auth.py``) and the
    settings GET (``routes/settings.py``, for the derived ``erp_login_active``
    status) — so there is exactly one resolution path. DB rows win where set;
    the deploy file is the default.
    """
    from sqlalchemy import select

    from backend.app.core.config import settings as app_settings
    from backend.app.models.settings import Settings

    env_defaults = load_erp_env_defaults(getattr(app_settings, "erp_config_file", None))

    result = await db.execute(select(Settings).where(Settings.key.in_(ERP_SETTING_KEYS)))
    db_rows = {s.key: s.value for s in result.scalars().all()}

    return {**env_defaults, **db_rows}


def _fetch_user_row(config: ErpConfig, username: str) -> dict | None:
    """Open a short-lived connection to the ERP DB and fetch the user row.

    Single parameterized query. Raises on any connection / query failure so
    the caller can classify it as UNREACHABLE. This is the driver boundary
    that tests mock.
    """
    import pymysql

    connect_kwargs: dict = {
        "host": config.host,
        "port": config.port,
        "user": config.user,
        "password": config.password,
        "database": config.database,
        "connect_timeout": 5,
        "read_timeout": 5,
        "cursorclass": pymysql.cursors.DictCursor,
    }
    if config.ssl:
        # Enable TLS. Verification depends on server cert trust; we request
        # TLS without pinning a CA here (LAN-scoped first-party DB).
        connect_kwargs["ssl"] = {"ssl": True}

    conn = pymysql.connect(**connect_kwargs)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT username, password_hash, role, active, locked_until FROM users WHERE username=%s",
                (username,),
            )
            return cursor.fetchone()
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close best effort
            pass


def _is_locked(locked_until) -> bool:
    """True when ``locked_until`` is set and still in the future (UTC compare)."""
    if locked_until is None:
        return False
    if isinstance(locked_until, datetime):
        lu = locked_until if locked_until.tzinfo is not None else locked_until.replace(tzinfo=timezone.utc)
        return lu > datetime.now(timezone.utc)
    # Non-datetime truthy value from an exotic driver config: treat as locked
    # (fail closed) rather than silently ignoring it.
    return bool(locked_until)


def _authenticate_erp_user_blocking(config: ErpConfig, username: str, password: str) -> ErpAuthResult:
    """Blocking ERP authentication (connect + query + bcrypt verify).

    Runs entirely in a worker thread. Only the connect/query is guarded as
    UNREACHABLE; the classification logic below raises normally if it has a
    bug, so a genuine outage is never confused with a rejection and vice
    versa.
    """
    try:
        row = _fetch_user_row(config, username)
    except Exception as exc:  # connection/operational/timeout -> transient
        logger.warning("ERP directory unreachable for %r: %s", username, exc)
        return ErpAuthResult(status=ErpAuthStatus.UNREACHABLE)

    if not row:
        return ErpAuthResult(status=ErpAuthStatus.REJECTED)

    stored_hash = row.get("password_hash")
    if not verify_bcrypt_password(password, stored_hash):
        return ErpAuthResult(status=ErpAuthStatus.REJECTED)

    active = bool(row.get("active"))
    if not active:
        return ErpAuthResult(status=ErpAuthStatus.REJECTED)

    if _is_locked(row.get("locked_until")):
        return ErpAuthResult(status=ErpAuthStatus.REJECTED)

    return ErpAuthResult(
        status=ErpAuthStatus.OK,
        erp_user=ErpUser(
            username=str(row.get("username") or username),
            role=str(row.get("role") or ""),
            active=active,
            password_hash=str(stored_hash),
        ),
    )


async def authenticate_erp_user(config: ErpConfig, username: str, password: str) -> ErpAuthResult:
    """Authenticate ``username``/``password`` against the ERP directory.

    Never blocks the event loop — the whole blocking run (connect, query,
    bcrypt verify) is dispatched to a worker thread.
    """
    if not username or not password:
        return ErpAuthResult(status=ErpAuthStatus.REJECTED)
    return await asyncio.to_thread(_authenticate_erp_user_blocking, config, username, password)
