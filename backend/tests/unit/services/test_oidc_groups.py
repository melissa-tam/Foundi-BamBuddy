"""Unit tests for the shared claim/role -> group mapping helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.group import Group
from backend.app.models.user import User
from backend.app.services.oidc_groups import apply_group_mapping, resolve_claim_groups


def _provider(groups_claim, group_mapping):
    gm = json.dumps(group_mapping) if isinstance(group_mapping, dict) else group_mapping
    return SimpleNamespace(groups_claim=groups_claim, group_mapping=gm)


class TestResolveClaimGroups:
    def test_unconfigured_returns_none(self):
        assert resolve_claim_groups(_provider(None, None), {"groups": ["x"]}) is None
        assert resolve_claim_groups(_provider("groups", None), {"groups": ["x"]}) is None

    def test_claim_missing_returns_none(self):
        prov = _provider("groups", {"farm-admins": "Administrators"})
        assert resolve_claim_groups(prov, {"sub": "abc"}) is None

    def test_string_claim_value(self):
        prov = _provider("groups", {"farm-admins": "Administrators"})
        assert resolve_claim_groups(prov, {"groups": "farm-admins"}) == ["Administrators"]

    def test_list_claim_value(self):
        prov = _provider("groups", {"farm-admins": "Administrators", "farm-ops": "Operators"})
        result = resolve_claim_groups(prov, {"groups": ["farm-admins", "farm-ops"]})
        assert set(result) == {"Administrators", "Operators"}

    def test_case_insensitive_mapping(self):
        prov = _provider("groups", {"Farm-Admins": "Administrators"})
        assert resolve_claim_groups(prov, {"groups": ["farm-admins"]}) == ["Administrators"]

    def test_unmatched_returns_empty_list(self):
        prov = _provider("groups", {"farm-admins": "Administrators"})
        # Claim present but no value maps -> [] (distinct from None = untouched).
        assert resolve_claim_groups(prov, {"groups": ["unknown"]}) == []

    def test_non_string_members_dropped(self):
        prov = _provider("groups", {"farm-admins": "Administrators"})
        assert resolve_claim_groups(prov, {"groups": [123, "farm-admins", None]}) == ["Administrators"]


async def _make_group(db: AsyncSession, name: str) -> Group:
    g = Group(name=name, description=name)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g


async def _make_user(db: AsyncSession, username: str, groups: list[Group]) -> User:
    u = User(username=username, auth_source="oidc", is_active=True, groups=groups)
    db.add(u)
    await db.commit()
    await db.refresh(u, attribute_names=["groups"])
    return u


@pytest.mark.asyncio
class TestApplyGroupMapping:
    async def test_replaces_membership_and_reports_change(self, db_session: AsyncSession):
        admins = await _make_group(db_session, "Administrators")
        ops = await _make_group(db_session, "Operators")
        user = await _make_user(db_session, "alice", [admins])

        changed = await apply_group_mapping(db_session, user, ["Operators"], source="test")
        await db_session.commit()
        await db_session.refresh(user, attribute_names=["groups"])

        assert changed is True
        assert {g.name for g in user.groups} == {"Operators"}
        assert ops.id != admins.id  # silence unused

    async def test_no_change_returns_false(self, db_session: AsyncSession):
        admins = await _make_group(db_session, "Administrators")
        user = await _make_user(db_session, "bob", [admins])

        changed = await apply_group_mapping(db_session, user, ["Administrators"], source="test")
        assert changed is False
        assert {g.name for g in user.groups} == {"Administrators"}

    async def test_empty_names_clears_groups(self, db_session: AsyncSession):
        admins = await _make_group(db_session, "Administrators")
        user = await _make_user(db_session, "carol", [admins])

        changed = await apply_group_mapping(db_session, user, [], source="test")
        await db_session.commit()
        await db_session.refresh(user, attribute_names=["groups"])

        assert changed is True
        assert user.groups == []
