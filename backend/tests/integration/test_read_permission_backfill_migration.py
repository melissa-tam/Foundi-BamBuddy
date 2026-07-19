"""Migration tests for maziggy/bambuddy-security #2 — read permission OWN/ALL backfill.

Pre-fix, ARCHIVES_READ / LIBRARY_READ / QUEUE_READ were flat "read all" flags.
Post-fix they split into OWN/ALL. The migration in seed_default_groups must:

  1. Rename legacy `archives:read` etc to `archives:read_all` on Administrators
     and to `archives:read_own` on every other role (fail-closed default).
  2. Backfill `_own` AND `_all` variants for the Administrators group on upgrade
     so an upgraded install matches a fresh install's permission set.
  3. Backfill `_own` variants for Operators and Viewers so they keep read access
     even if their stored row didn't carry the legacy flag.

These regressions are the failure shape Maziggy hit on a live upgrade — the
admin role ended up missing queue:read_own AND queue:read after migration.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core import database as _database_module
from backend.app.core.database import seed_default_groups
from backend.app.models.group import Group

_READ_FLAGS = frozenset(
    {
        "archives:read",
        "archives:read_own",
        "archives:read_all",
        "library:read",
        "library:read_own",
        "library:read_all",
        "queue:read",
        "queue:read_own",
        "queue:read_all",
    }
)


async def _strip_and_set(group_name: str, extra: list[str] | None = None) -> None:
    """Strip every read flag from ``group_name`` then add ``extra`` flags.

    Simulates a pre-migration state where the group either had only the
    legacy flat permission (set ``extra=['archives:read']``) or no read
    permission at all (set ``extra=None``).
    """
    async with _database_module.async_session() as session:
        grp = (await session.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
        assert grp is not None, f"group {group_name} not pre-seeded"
        stripped = [p for p in (grp.permissions or []) if p not in _READ_FLAGS]
        stripped.extend(extra or [])
        grp.permissions = stripped
        await session.commit()


async def _get_perms(group_name: str) -> set[str]:
    async with _database_module.async_session() as session:
        grp = (await session.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
        assert grp is not None
        return set(grp.permissions or [])


async def _get_perms_list(group_name: str) -> list[str]:
    """Ordered permission list (order-sensitive assertions for the seed-sync)."""
    async with _database_module.async_session() as session:
        grp = (await session.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
        assert grp is not None
        return list(grp.permissions or [])


async def _set_perms(group_name: str, perms: list[str], *, is_system: bool | None = None) -> None:
    """Overwrite a group's stored permission list (and optionally is_system).

    Simulates a pre-existing group row whose permission set predates newer
    DEFAULT_GROUPS grants (the shape the additive seed-sync must reconcile).
    """
    async with _database_module.async_session() as session:
        grp = (await session.execute(select(Group).where(Group.name == group_name))).scalar_one_or_none()
        assert grp is not None, f"group {group_name} not pre-seeded"
        grp.permissions = list(perms)
        if is_system is not None:
            grp.is_system = is_system
        await session.commit()


# Note: ``async_client`` is depended upon (even though unused) so pytest-asyncio
# uses the same event loop the conftest fixture uses for async_session(). Without
# it, calling ``async_session()`` twice in one test trips an asyncpg
# "got Future attached to a different loop" RuntimeError.


class TestReadPermissionMigration:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_legacy_archives_read_renamed_to_all_for_administrators(self, async_client: AsyncClient):
        """Existing Administrators group with legacy `archives:read` → gets
        `archives:read_all` after seed_default_groups runs, and gets the
        `_own` companion backfilled too."""
        await seed_default_groups()
        await _strip_and_set("Administrators", extra=["archives:read"])

        await seed_default_groups()

        perms = await _get_perms("Administrators")
        # Rename happened: legacy renamed to _all
        assert "archives:read_all" in perms
        # Backfill also added _own so fresh install and upgraded install match
        assert "archives:read_own" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_administrators_backfill_adds_all_six_read_flags(self, async_client: AsyncClient):
        """Even with NO legacy flags present, Administrators ends up with both
        OWN and ALL variants for archives / library / queue after the backfill
        pass. This is the case Maziggy hit — admin missing `queue:read_own`
        after upgrade."""
        await seed_default_groups()
        await _strip_and_set("Administrators")

        await seed_default_groups()

        perms = await _get_perms("Administrators")
        for needed in (
            "archives:read_own",
            "archives:read_all",
            "library:read_own",
            "library:read_all",
            "queue:read_own",
            "queue:read_all",
        ):
            assert needed in perms, f"{needed} must be backfilled for Administrators"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operators_backfill_adds_own_read_flags(self, async_client: AsyncClient):
        """Operators with no read flags get the _OWN variants backfilled, AND
        the shared-surface _ALL grants the farm remediation added to the seed
        (queue:read_all + library:read_all — the shared farm queue/library).

        archives stay own-scoped for Operators: archives:read_all is NOT a
        shared-surface grant and must remain absent (the IDOR closure there is
        unchanged)."""
        await seed_default_groups()
        await _strip_and_set("Operators")

        await seed_default_groups()

        perms = await _get_perms("Operators")
        assert "archives:read_own" in perms
        assert "library:read_own" in perms
        assert "queue:read_own" in perms
        # Archives are still own-scoped for Operators (not a shared surface).
        assert "archives:read_all" not in perms
        # Queue + library reads ARE shared-surface for Operators now (farm
        # remediation: operators see the whole admin-created queue/library).
        assert "library:read_all" in perms
        assert "queue:read_all" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operators_legacy_archives_read_renamed_to_own(self, async_client: AsyncClient):
        """Pre-PR Operators with legacy `archives:read` get the _OWN rename
        (fail-closed — close the IDOR, the operator can re-request _ALL via
        admin if cross-user visibility is genuinely needed)."""
        await seed_default_groups()
        await _strip_and_set("Operators", extra=["archives:read"])

        await seed_default_groups()

        perms = await _get_perms("Operators")
        assert "archives:read_own" in perms
        assert "archives:read_all" not in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_administrators_legacy_archives_read_retained(self, async_client: AsyncClient):
        """Admin keeps the LEGACY `archives:read` flag — the frontend gates
        download / preview UI on it (ArchivesPage / FileManagerPage), and
        removing it on rename was leaving admin with no visible download
        buttons after upgrade. The new API gates use the _ALL variant which
        the backfill also ensures is present."""
        await seed_default_groups()
        await _strip_and_set("Administrators", extra=["archives:read"])

        await seed_default_groups()

        perms = await _get_perms("Administrators")
        # Both the legacy flag (for the UI) and the _all variant (for the API)
        # must coexist on admin.
        assert "archives:read" in perms
        assert "archives:read_all" in perms
        assert "archives:read_own" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_administrators_backfill_adds_legacy_read_flags(self, async_client: AsyncClient):
        """Admin with NO read flags at all (hand-edited or stripped role) ends
        up with the legacy `archives:read` / `queue:read` / `library:read`
        backfilled — so the UI gates work — alongside the OWN/ALL split."""
        await seed_default_groups()
        await _strip_and_set("Administrators")

        await seed_default_groups()

        perms = await _get_perms("Administrators")
        for needed in (
            "archives:read",
            "library:read",
            "queue:read",
            "archives:read_own",
            "archives:read_all",
            "library:read_own",
            "library:read_all",
            "queue:read_own",
            "queue:read_all",
        ):
            assert needed in perms, f"{needed} must be backfilled for Administrators"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_administrators_orca_cloud_auth_backfilled(self, async_client: AsyncClient):
        """Admin without `orca_cloud:auth` (older custom edit) gets it
        backfilled — matches the fresh-install default."""
        await seed_default_groups()
        async with _database_module.async_session() as session:
            grp = (await session.execute(select(Group).where(Group.name == "Administrators"))).scalar_one()
            grp.permissions = [p for p in (grp.permissions or []) if p != "orca_cloud:auth"]
            await session.commit()

        await seed_default_groups()

        perms = await _get_perms("Administrators")
        assert "orca_cloud:auth" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_operators_orca_cloud_auth_backfilled(self, async_client: AsyncClient):
        """Operators on upgraded installs get `orca_cloud:auth` backfilled
        (the new default — needed for the Slice modal's Orca Cloud preset
        picker)."""
        await seed_default_groups()
        async with _database_module.async_session() as session:
            grp = (await session.execute(select(Group).where(Group.name == "Operators"))).scalar_one()
            grp.permissions = [p for p in (grp.permissions or []) if p != "orca_cloud:auth"]
            await session.commit()

        await seed_default_groups()

        perms = await _get_perms("Operators")
        assert "orca_cloud:auth" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_viewers_do_not_get_orca_cloud_auth(self, async_client: AsyncClient):
        """Viewers stay read-only — orca_cloud:auth is not added by the
        backfill (matches the fresh-install Viewers bootstrap, which
        intentionally excludes cloud-auth permissions)."""
        await seed_default_groups()
        async with _database_module.async_session() as session:
            grp = (await session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
            grp.permissions = [p for p in (grp.permissions or []) if p != "orca_cloud:auth"]
            await session.commit()

        await seed_default_groups()

        perms = await _get_perms("Viewers")
        assert "orca_cloud:auth" not in perms


class TestSeedGroupSync:
    """Generic additive seed-sync (D10): pre-existing SYSTEM groups get newly
    seeded DEFAULT_GROUPS permissions back-filled at startup, without ever
    removing a stored (possibly admin-customized) permission.

    This closes the gap behind incident-3: the production Operators group
    predated the farm SKU / production-run / eject and shared-queue grants, and
    seed_default_groups only ever *created* missing groups — it never topped up
    an existing system group with permissions added to the seed later.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_seed_perms_backfilled_in_order(self, async_client: AsyncClient):
        """A system Operators row that is a strict subset of the seed gains
        exactly the missing seed perms — current order preserved at the front,
        missing perms appended in seed order."""
        from backend.app.core.permissions import DEFAULT_GROUPS

        await seed_default_groups()
        base = ["printers:read", "websocket:connect"]  # both valid seed perms
        await _set_perms("Operators", base)

        await seed_default_groups()

        perms = await _get_perms_list("Operators")
        seed_perms = DEFAULT_GROUPS["Operators"]["permissions"]
        # Current order preserved at the front.
        assert perms[: len(base)] == base
        # Missing seed perms appended in seed order.
        appended = perms[len(base) :]
        expected_missing = [p for p in seed_perms if p not in set(base)]
        assert appended == expected_missing
        # Net set is exactly the seed (base was a strict subset), no dupes.
        assert set(perms) == set(seed_perms)
        assert len(perms) == len(set(perms))
        # The three shared-surface grants specifically landed.
        assert {
            "queue:read_all",
            "queue:update_all",
            "library:read_all",
            "skus:read",
            "production_runs:read",
            "eject_profiles:read",
        } <= set(perms)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_custom_extra_perms_preserved(self, async_client: AsyncClient):
        """A permission an admin added that is NOT in the seed survives the
        sync (the pass only ADDS; it never prunes)."""
        await seed_default_groups()
        # settings:update is admin-level and NOT part of the Operators seed.
        base = ["printers:read", "websocket:connect", "settings:update"]
        await _set_perms("Operators", base)

        await seed_default_groups()

        perms = await _get_perms("Operators")
        assert "settings:update" in perms  # custom extra preserved
        assert "skus:read" in perms  # seed perm still backfilled alongside
        assert "queue:read_all" in perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_non_system_group_with_default_name_untouched(self, async_client: AsyncClient):
        """A group carrying a DEFAULT_GROUPS name but is_system=False (a custom
        role) is skipped by the sync — those are curated by hand."""
        await seed_default_groups()
        base = ["printers:read", "websocket:connect"]
        await _set_perms("Operators", base, is_system=False)

        await seed_default_groups()

        perms = await _get_perms("Operators")
        # The additive seed-sync is is_system-gated: NONE of the farm/shared
        # grants that only it would inject appear on a non-system row. (The
        # pre-existing one-off, name-targeted backfills — read_own, orca_cloud
        # — are is_system-agnostic and out of this sync's scope, so we assert
        # on the perms the sync uniquely owns rather than full equality.)
        for farm_perm in (
            "skus:read",
            "queue:read_all",
            "queue:update_all",
            "library:read_all",
            "production_runs:read",
            "eject_profiles:read",
        ):
            assert farm_perm not in perms, f"{farm_perm} leaked into a non-system group"
        # Base perms preserved (the sync never removes anything).
        assert {"printers:read", "websocket:connect"} <= perms

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_second_run_is_noop(self, async_client: AsyncClient):
        """Running the sync twice yields an identical list — no re-append, no
        duplicates."""
        await seed_default_groups()
        await _set_perms("Operators", ["printers:read", "websocket:connect"])

        await seed_default_groups()
        after_first = await _get_perms_list("Operators")

        await seed_default_groups()
        after_second = await _get_perms_list("Operators")

        assert after_second == after_first
        assert len(after_second) == len(set(after_second))

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_fresh_create_path_still_seeds_full_set(self, async_client: AsyncClient):
        """The create-missing path is unaffected: a fresh install (the
        async_client fixture already seeded onto an empty DB) has the FULL
        Operators seed, including the three new shared-surface grants."""
        from backend.app.core.permissions import DEFAULT_GROUPS

        perms = await _get_perms("Operators")
        assert perms == set(DEFAULT_GROUPS["Operators"]["permissions"])
        assert {"queue:read_all", "queue:update_all", "library:read_all"} <= perms
