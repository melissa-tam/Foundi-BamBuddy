"""Integration tests for ERP directory login on /auth/login.

The ERP driver is mocked at ``authenticate_erp_user`` (the service entry the
route hook awaits), so no real ERP DB is touched. Config parsing, hash
mirroring, group sync, and the offline-cache fallback run for real.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import bcrypt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import authenticate_user, authenticate_user_by_email
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services.erp_directory import ErpAuthResult, ErpAuthStatus, ErpUser

_PW = "s3cret"  # noqa: S105 - test fixture
_HASH = bcrypt.hashpw(_PW.encode(), bcrypt.gensalt(rounds=10, prefix=b"2a")).decode()
_HASH2 = bcrypt.hashpw(b"rotated", bcrypt.gensalt(rounds=10, prefix=b"2a")).decode()

_TARGET = "backend.app.services.erp_directory.authenticate_erp_user"


async def _seed_erp_settings(db: AsyncSession, **overrides) -> None:
    # No enable flag anymore — seeding the connection config (host/name/user) is
    # what makes ERP login active (a per-instance DB override of the deploy file).
    defaults = {
        "erp_db_host": "erp.example",
        "erp_db_name": "Foundi_management_system",
        "erp_db_user": "reader",
        "erp_db_password": "x",  # noqa: S106 - test fixture
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        db.add(Settings(key=key, value=value))
    await db.commit()


async def _enable_auth(async_client: AsyncClient, username: str, password: str = "AdminPass1!") -> None:
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": username, "admin_password": password},
    )


def _ok(username="melissa.tam1", role="VIEWER", active=True, password_hash=_HASH):
    return ErpAuthResult(
        status=ErpAuthStatus.OK,
        erp_user=ErpUser(username=username, role=role, active=active, password_hash=password_hash),
    )


async def _get_user(db: AsyncSession, username: str) -> User | None:
    from sqlalchemy.orm import selectinload

    # The route handler mutates a DIFFERENT session; expire ours so we read the
    # committed state rather than a cached identity-map copy.
    db.expire_all()
    res = await db.execute(select(User).where(User.username == username).options(selectinload(User.groups)))
    return res.scalar_one_or_none()


@pytest.mark.asyncio
@pytest.mark.integration
class TestErpProvisioningAndSync:
    async def test_first_login_provisions_with_hash_and_groups(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)

        with patch(_TARGET, new=AsyncMock(return_value=_ok(role="ADMIN"))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()

        user = await _get_user(db_session, "melissa.tam1")
        assert user is not None
        assert user.auth_source == "erp"
        assert user.password_hash == _HASH  # mirrored verbatim, not recomputed
        assert user.email is None
        assert {g.name for g in user.groups} == {"Administrators"}

    async def test_hash_mirror_updates_after_erp_rotation(self, async_client: AsyncClient, db_session: AsyncSession):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)

        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        user = await _get_user(db_session, "melissa.tam1")
        assert user.password_hash == _HASH

        # ERP rotated the credential -> next OK login mirrors the new hash.
        with patch(_TARGET, new=AsyncMock(return_value=_ok(password_hash=_HASH2))):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": "rotated"})
        await db_session.refresh(user)
        assert user.password_hash == _HASH2

    async def test_role_change_resyncs_groups(self, async_client: AsyncClient, db_session: AsyncSession):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)

        with patch(_TARGET, new=AsyncMock(return_value=_ok(role="VIEWER"))):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        user = await _get_user(db_session, "melissa.tam1")
        assert {g.name for g in user.groups} == {"Viewers"}

        with patch(_TARGET, new=AsyncMock(return_value=_ok(role="ADMIN"))):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        user2 = await _get_user(db_session, "melissa.tam1")
        assert {g.name for g in user2.groups} == {"Administrators"}


@pytest.mark.asyncio
@pytest.mark.integration
class TestErpZeroConfigEnvFile:
    """Zero day-2: a freshly-deployed instance authenticates ERP users from the
    bundled ``erp.env`` connection file alone — NO DB settings rows and NO
    enable flag. This is the whole point of the flag removal + deploy file."""

    async def test_login_from_env_file_with_no_db_rows(
        self, async_client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
    ):
        await _enable_auth(async_client, "erpadm")
        # No _seed_erp_settings() -> the settings table has zero erp_* rows.
        env = tmp_path / "erp.env"
        env.write_text(
            "erp_db_host=erp.example\n"
            "erp_db_name=Foundi_management_system\n"
            "erp_db_user=farm_reader\n"
            "erp_db_password=x\n"
        )
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "erp_config_file", env, raising=False)

        with patch(_TARGET, new=AsyncMock(return_value=_ok(role="ADMIN"))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 200, resp.text

        user = await _get_user(db_session, "melissa.tam1")
        assert user is not None
        assert user.auth_source == "erp"
        assert {g.name for g in user.groups} == {"Administrators"}

    async def test_no_env_file_and_no_db_rows_skips_erp(
        self, async_client: AsyncClient, db_session: AsyncSession, tmp_path, monkeypatch
    ):
        # No config anywhere -> ERP branch is skipped and an unknown ERP user
        # falls through to the generic 401 (upstream/OSS behaviour preserved).
        await _enable_auth(async_client, "erpadm")
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "erp_config_file", tmp_path / "absent.env", raising=False)

        called = AsyncMock(return_value=_ok())
        with patch(_TARGET, new=called):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 401
        called.assert_not_awaited()  # ERP never consulted when no config resolves


@pytest.mark.asyncio
@pytest.mark.integration
class TestErpOfflineCache:
    async def test_unreachable_with_correct_cached_password_logs_in(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        # Provision first (OK), then simulate an outage.
        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})

        with patch(_TARGET, new=AsyncMock(return_value=ErpAuthResult(status=ErpAuthStatus.UNREACHABLE))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 200, resp.text

    async def test_unreachable_with_wrong_password_denied(self, async_client: AsyncClient, db_session: AsyncSession):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})

        with patch(_TARGET, new=AsyncMock(return_value=ErpAuthResult(status=ErpAuthStatus.UNREACHABLE))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": "wrong"})
        assert resp.status_code == 401

    async def test_unreachable_no_prior_user_denied(self, async_client: AsyncClient, db_session: AsyncSession):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        with patch(_TARGET, new=AsyncMock(return_value=ErpAuthResult(status=ErpAuthStatus.UNREACHABLE))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "nobody.here", "password": _PW})
        assert resp.status_code == 401
        assert await _get_user(db_session, "nobody.here") is None

    async def test_unreachable_inactive_mirror_denied_despite_correct_password(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """A locally deactivated ERP mirror (operator kill switch) must NOT get
        a token from the offline cache during an ERP outage — mirrors
        authenticate_user()'s is_active semantics at login time."""
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        # Provision the mirror, then deactivate it locally.
        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        user = await _get_user(db_session, "melissa.tam1")
        user.is_active = False
        await db_session.commit()

        with patch(_TARGET, new=AsyncMock(return_value=ErpAuthResult(status=ErpAuthStatus.UNREACHABLE))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
class TestErpSecurityInvariants:
    async def test_rejected_denies_even_with_matching_cached_hash(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """KEY property: while the ERP is reachable and says REJECTED, a stale
        cached hash that matches the supplied password must NOT authenticate."""
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        # Provision the cached hash for _PW.
        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        user = await _get_user(db_session, "melissa.tam1")
        assert user.password_hash == _HASH  # cache holds the matching hash

        # ERP now rejects. Correct cached password must still be denied.
        with patch(_TARGET, new=AsyncMock(return_value=ErpAuthResult(status=ErpAuthStatus.REJECTED))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
        assert resp.status_code == 401

    async def test_same_username_local_user_not_hijacked(self, async_client: AsyncClient, db_session: AsyncSession):
        # The admin created at setup is a local account named "erpadm".
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        before = await _get_user(db_session, "erpadm")
        original_hash = before.password_hash

        # ERP claims OK for "erpadm" with a different password. The hook must
        # refuse to hijack the local account: login with the ERP-only password
        # falls through to local auth and fails.
        with patch(_TARGET, new=AsyncMock(return_value=_ok(username="erpadm", role="ADMIN"))):
            resp = await async_client.post("/api/v1/auth/login", json={"username": "erpadm", "password": "erp-only-pw"})
        assert resp.status_code == 401

        after = await _get_user(db_session, "erpadm")
        assert after.auth_source == "local"  # untouched
        assert after.password_hash == original_hash  # not mirrored over

    async def test_generic_local_path_closed_for_erp_users(self, async_client: AsyncClient, db_session: AsyncSession):
        """authenticate_user / authenticate_user_by_email must never authenticate
        an auth_source=="erp" user even with the correct mirrored password."""
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        with patch(_TARGET, new=AsyncMock(return_value=_ok())):
            await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})

        assert await authenticate_user(db_session, "melissa.tam1", _PW) is None
        assert await authenticate_user_by_email(db_session, "melissa.tam1", _PW) is None


async def _erp_login_token(async_client: AsyncClient, role: str = "VIEWER") -> str:
    """Log in as the ERP user (mocked OK) and return the bearer token."""
    with patch(_TARGET, new=AsyncMock(return_value=_ok(role=role))):
        resp = await async_client.post("/api/v1/auth/login", json={"username": "melissa.tam1", "password": _PW})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.mark.asyncio
@pytest.mark.integration
class TestErpReauthEndpoints:
    """Sibling class sweep: endpoints that feed ``current_user.password_hash``
    into ``verify_password`` must not 500 on ERP users' mirrored bcrypt hashes.
    Before the hash-agnostic dispatch in core/auth.py, passlib's pbkdf2-only
    context raised ``UnknownHashError`` -> HTTP 500 at each of these sites."""

    async def test_erp_user_disable_email_otp_correct_password(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        token = await _erp_login_token(async_client)

        resp = await async_client.post(
            "/api/v1/auth/2fa/email/disable",
            json={"password": _PW},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    async def test_erp_user_disable_email_otp_wrong_password_401_not_500(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        token = await _erp_login_token(async_client)

        resp = await async_client.post(
            "/api/v1/auth/2fa/email/disable",
            json={"password": "wrong"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401, resp.text

    async def test_erp_admin_disable_2fa_correct_password(self, async_client: AsyncClient, db_session: AsyncSession):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        # ERP ADMIN role -> Administrators group -> USERS_UPDATE permission.
        token = await _erp_login_token(async_client, role="ADMIN")
        target = await _get_user(db_session, "erpadm")

        resp = await async_client.request(
            "DELETE",
            f"/api/v1/auth/2fa/admin/{target.id}",
            json={"admin_password": _PW},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    async def test_erp_admin_disable_2fa_wrong_password_401_not_500(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        token = await _erp_login_token(async_client, role="ADMIN")
        target = await _get_user(db_session, "erpadm")

        resp = await async_client.request(
            "DELETE",
            f"/api/v1/auth/2fa/admin/{target.id}",
            json={"admin_password": "wrong"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401, resp.text

    async def test_erp_user_change_password_403(self, async_client: AsyncClient, db_session: AsyncSession):
        """ERP users must never overwrite the mirrored hash with a local pbkdf2
        hash — that would desync the offline credential cache."""
        await _enable_auth(async_client, "erpadm")
        await _seed_erp_settings(db_session)
        token = await _erp_login_token(async_client)

        resp = await async_client.post(
            "/api/v1/users/me/change-password",
            json={"current_password": _PW, "new_password": "NewPass1!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, resp.text
        # Mirror untouched.
        user = await _get_user(db_session, "melissa.tam1")
        assert user.password_hash == _HASH

    async def test_local_user_change_password_still_works(self, async_client: AsyncClient, db_session: AsyncSession):
        """Regression: pbkdf2 local users still pass the verify_password path."""
        await _enable_auth(async_client, "erpadm")
        login = await async_client.post("/api/v1/auth/login", json={"username": "erpadm", "password": "AdminPass1!"})
        token = login.json()["access_token"]

        resp = await async_client.post(
            "/api/v1/users/me/change-password",
            json={"current_password": "AdminPass1!", "new_password": "NewPass1!"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
