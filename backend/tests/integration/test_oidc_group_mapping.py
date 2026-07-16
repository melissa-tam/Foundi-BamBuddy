"""Integration tests for OIDC claim->group mapping + ERP identity convergence.

Exercises the real OIDC callback (mocked IdP HTTP + JWKS) with a groups claim
in the id_token, plus the convergence path that links an OIDC identity onto an
existing ERP-provisioned user instead of minting a duplicate.
"""

from __future__ import annotations

import base64
import secrets
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.auth_ephemeral import AuthEphemeralToken
from backend.app.models.oidc_provider import UserOIDCLink
from backend.app.models.user import User


def _make_rsa_key():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    pub = priv.public_key().public_numbers()

    def _b64url(n: int, length: int) -> str:
        return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": "test-kid-1",
                "n": _b64url(pub.n, 256),
                "e": _b64url(pub.e, 3),
            }
        ]
    }
    return pem, jwks


class _MockResp:
    def __init__(self, data):
        self._data = data
        self.status_code = 200
        self.is_success = True
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def _mock_httpx_factory(discovery_doc, jwks_data, token_response):
    class _MockHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            if "jwks" in url:
                return _MockResp(jwks_data)
            return _MockResp(discovery_doc)

        async def post(self, url, **kwargs):
            return _MockResp(token_response)

    return _MockHttpxClient


async def _trigger_callback(
    async_client: AsyncClient,
    db_session: AsyncSession,
    provider_id: int,
    issuer: str,
    client_id: str,
    private_pem: bytes,
    jwks_data: dict,
    *,
    sub: str,
    extra_claims: dict,
) -> str:
    nonce = secrets.token_urlsafe(16)
    state = secrets.token_urlsafe(32)
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": issuer,
        "aud": client_id,
        "nonce": nonce,
        "iat": now,
        "exp": now + 300,
    }
    payload.update(extra_claims)
    id_token = pyjwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-kid-1"})

    db_session.add(
        AuthEphemeralToken(
            token=state,
            token_type="oidc_state",
            provider_id=provider_id,
            nonce=nonce,
            code_verifier=secrets.token_urlsafe(48),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.commit()

    discovery = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/auth",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
    }
    token_response = {"access_token": "mock-access", "token_type": "Bearer", "id_token": id_token}

    with patch(
        "backend.app.api.routes.mfa.httpx.AsyncClient",
        _mock_httpx_factory(discovery, jwks_data, token_response),
    ):
        resp = await async_client.get(
            f"/api/v1/auth/oidc/callback?code=test-code&state={state}", follow_redirects=False
        )
    assert resp.status_code == 302, resp.text
    location = resp.headers.get("location", "")
    assert "oidc_token=" in location, f"expected oidc_token, got {location}"
    return location


async def _admin_headers(async_client: AsyncClient, username: str) -> dict:
    await async_client.post(
        "/api/v1/auth/setup",
        json={"auth_enabled": True, "admin_username": username, "admin_password": "AdminPass1!"},
    )
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": "AdminPass1!"})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _create_provider(async_client: AsyncClient, headers: dict, issuer: str, client_id: str, **overrides) -> int:
    body = {
        "name": overrides.pop("name", "GroupIdP"),
        "issuer_url": issuer,
        "client_id": client_id,
        "client_secret": "test-secret",  # noqa: S106 - test fixture
        "scopes": "openid email profile",
        "is_enabled": True,
        "auto_create_users": True,
    }
    body.update(overrides)
    resp = await async_client.post("/api/v1/auth/oidc/providers", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _user_by_username(db: AsyncSession, username: str) -> User | None:
    # The callback runs on a different session; expire ours to read committed state.
    db.expire_all()
    res = await db.execute(
        select(User).where(func.lower(User.username) == username.lower()).options(selectinload(User.groups))
    )
    return res.scalar_one_or_none()


@pytest.mark.asyncio
@pytest.mark.integration
class TestOidcGroupMapping:
    async def test_auto_create_lands_in_mapped_group(self, async_client: AsyncClient, db_session: AsyncSession):
        pem, jwks = _make_rsa_key()
        issuer, client_id = "https://idp.gm1.example.com", "gm1-client"
        headers = await _admin_headers(async_client, "gm1adm")
        pid = await _create_provider(
            async_client,
            headers,
            issuer,
            client_id,
            groups_claim="groups",
            group_mapping={"farm-admins": "Administrators"},
        )

        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm1-sub",
            extra_claims={"email": "mapuser@example.com", "email_verified": True, "groups": ["farm-admins"]},
        )
        user = await _user_by_username(db_session, "mapuser")
        assert user is not None
        assert {g.name for g in user.groups} == {"Administrators"}

    async def test_provider_without_mapping_uses_default_fallback(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        pem, jwks = _make_rsa_key()
        issuer, client_id = "https://idp.gm2.example.com", "gm2-client"
        headers = await _admin_headers(async_client, "gm2adm")
        pid = await _create_provider(async_client, headers, issuer, client_id)  # no groups_claim/mapping

        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm2-sub",
            extra_claims={"email": "plain@example.com", "email_verified": True, "groups": ["farm-admins"]},
        )
        user = await _user_by_username(db_session, "plain")
        # Unchanged behavior: Viewers fallback (no default_group_id configured).
        assert {g.name for g in user.groups} == {"Viewers"}

    async def test_linked_user_resync_on_role_change(self, async_client: AsyncClient, db_session: AsyncSession):
        pem, jwks = _make_rsa_key()
        issuer, client_id = "https://idp.gm3.example.com", "gm3-client"
        headers = await _admin_headers(async_client, "gm3adm")
        pid = await _create_provider(
            async_client,
            headers,
            issuer,
            client_id,
            groups_claim="groups",
            group_mapping={"farm-admins": "Administrators", "farm-viewers": "Viewers"},
        )

        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm3-sub",
            extra_claims={"email": "role@example.com", "email_verified": True, "groups": ["farm-viewers"]},
        )
        user = await _user_by_username(db_session, "role")
        assert {g.name for g in user.groups} == {"Viewers"}

        # Same sub -> existing link -> re-sync from the new claim value.
        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm3-sub",
            extra_claims={"email": "role@example.com", "email_verified": True, "groups": ["farm-admins"]},
        )
        user2 = await _user_by_username(db_session, "role")
        assert {g.name for g in user2.groups} == {"Administrators"}

    async def test_claim_absent_leaves_groups_untouched(self, async_client: AsyncClient, db_session: AsyncSession):
        pem, jwks = _make_rsa_key()
        issuer, client_id = "https://idp.gm4.example.com", "gm4-client"
        headers = await _admin_headers(async_client, "gm4adm")
        pid = await _create_provider(
            async_client,
            headers,
            issuer,
            client_id,
            groups_claim="groups",
            group_mapping={"farm-admins": "Administrators"},
        )

        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm4-sub",
            extra_claims={"email": "keep@example.com", "email_verified": True, "groups": ["farm-admins"]},
        )
        assert {g.name for g in (await _user_by_username(db_session, "keep")).groups} == {"Administrators"}

        # Second login without the groups claim must leave membership untouched.
        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="gm4-sub",
            extra_claims={"email": "keep@example.com", "email_verified": True},
        )
        assert {g.name for g in (await _user_by_username(db_session, "keep")).groups} == {"Administrators"}


@pytest.mark.asyncio
@pytest.mark.integration
class TestOidcErpConvergence:
    async def test_convergence_links_to_erp_user_not_duplicate(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        pem, jwks = _make_rsa_key()
        issuer, client_id = "https://idp.conv.example.com", "conv-client"
        headers = await _admin_headers(async_client, "convadm")
        pid = await _create_provider(async_client, headers, issuer, client_id)  # auto_create_users=True

        # Pre-existing ERP-provisioned user with a bare username (no email).
        erp_user = User(username="melissa.tam1", email=None, auth_source="erp", is_active=True)
        db_session.add(erp_user)
        await db_session.commit()
        await db_session.refresh(erp_user)
        erp_id = erp_user.id

        # OIDC login where preferred_username matches the ERP username and the
        # email does NOT collide with any existing user.
        await _trigger_callback(
            async_client,
            db_session,
            pid,
            issuer,
            client_id,
            pem,
            jwks,
            sub="conv-sub",
            extra_claims={
                "email": "melissa.tam@corp.example.com",
                "email_verified": True,
                "preferred_username": "melissa.tam1",
            },
        )

        # No duplicate account was minted.
        count = await db_session.scalar(select(func.count(User.id)).where(User.username.like("melissa.tam1%")))
        assert count == 1

        # The OIDC identity linked onto the ERP user.
        link = await db_session.execute(select(UserOIDCLink).where(UserOIDCLink.provider_user_id == "conv-sub"))
        link_row = link.scalar_one()
        assert link_row.user_id == erp_id
