"""Structural pins: one owner per test resource.

``backend/tests/_fixtures/`` owns the database engine, the session factories, the
steerable clock and the parsed view of ``backend/app``. Every rule below fails
the moment a SECOND owner appears — a new file that builds its own engine,
truncates its own tables, hand-rolls another ``_Clock``, or assigns a directory
onto the live ``settings`` singleton.

**The allowlists only ever shrink.** They enumerate the sites that already
existed when ``_fixtures`` took ownership; they are a burn-down list, not a
licence. Two assertions enforce that in both directions: a file that offends and
is NOT listed fails immediately, and a listed file that has stopped offending
fails until its entry is deleted. Nothing is ever added to a list to make a new
test pass — the fixture is imported instead:

    from backend.tests._fixtures.db import create_memory_engine, force_sqlite_dialect
    from backend.tests._fixtures.clock import FakeClock

The data-root pin is not here but in ``conftest.py`` (``assert_one_data_root``):
it has to run after EVERY test, and an autouse fixture in this module would only
cover this module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.tests._fixtures.ast_tree import ParsedModule, ParsedTree

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "backend" / "tests"
# The owner. Everything under it is allowed to do all of the below — that is the
# point of it.
OWNER_PREFIX = "backend/tests/_fixtures/"

# --------------------------------------------------------------------------
# Allowlists — sites that predate _fixtures/ ownership. DELETE, never extend.
# --------------------------------------------------------------------------

CREATE_ASYNC_ENGINE_ALLOWLIST = frozenset(
    {
        "backend/tests/integration/test_security.py",
        "backend/tests/unit/services/test_foreign_print_accounting.py",
        "backend/tests/unit/services/test_hms_event.py",
        "backend/tests/unit/services/test_printer_incidents.py",
        "backend/tests/unit/services/test_queue_transitions.py",
        "backend/tests/unit/services/test_scheduler_library_file_missing.py",
        "backend/tests/unit/services/test_user_deletion.py",
        "backend/tests/unit/test_ams_wedged_idle_column_migration.py",
        "backend/tests/unit/test_backup_group_split_column_migration.py",
        "backend/tests/unit/test_blank_tagless_identity_repair.py",
        "backend/tests/unit/test_cancellation_cascade_recovery_migration.py",
        "backend/tests/unit/test_cli.py",
        "backend/tests/unit/test_cooldown_escalation_column_migration.py",
        "backend/tests/unit/test_db_dialect.py",
        "backend/tests/unit/test_eject_profile_bed_drop_migration.py",
        "backend/tests/unit/test_eject_profile_dwell_jitter_migration.py",
        "backend/tests/unit/test_eject_profile_sweep_columns_migration.py",
        "backend/tests/unit/test_id_reuse_autoincrement_migration.py",
        "backend/tests/unit/test_ldap_migration.py",
        "backend/tests/unit/test_library_file_type_backfill_migration.py",
        "backend/tests/unit/test_location_migration.py",
        "backend/tests/unit/test_migrations_autonomy_posture.py",
        "backend/tests/unit/test_migrations_cooldown_fans.py",
        "backend/tests/unit/test_migrations_spool_lifecycle.py",
        "backend/tests/unit/test_model_geometry_cooldown_hold_migration.py",
        "backend/tests/unit/test_model_geometry_registry_seed_migration.py",
        "backend/tests/unit/test_model_geometry_z_travel_migration.py",
        "backend/tests/unit/test_orphan_auth_cleanup_migration.py",
        "backend/tests/unit/test_pending_ams_mapping_cutover_migration.py",
        "backend/tests/unit/test_phantom_print_hardening.py",
        "backend/tests/unit/test_pool_pinned_farm_units_cutover_migration.py",
        "backend/tests/unit/test_power_loss_recovery_column_migration.py",
        "backend/tests/unit/test_print_log_backfill_migration.py",
        "backend/tests/unit/test_queue_assigned_template_pool_label_migration.py",
        "backend/tests/unit/test_runout_reclaim_repair_migration.py",
        "backend/tests/unit/test_runtime_tracking_pause.py",
        "backend/tests/unit/test_scheduler_cleanup_library.py",
        "backend/tests/unit/test_scheduler_clear_plate.py",
        "backend/tests/unit/test_scheduler_filament_deficit.py",
        "backend/tests/unit/test_scheduler_pin_contract.py",
        "backend/tests/unit/test_scheduler_watchdog.py",
        "backend/tests/unit/test_settings_dedupe_migration.py",
        "backend/tests/unit/test_sponsor_toast_state_drop_migration.py",
        "backend/tests/unit/test_spool_recovery_self_healed_column_migration.py",
        "backend/tests/unit/test_tagless_default_identity_migration.py",
        "backend/tests/unit/test_user_print_template_rename_migration.py",
        "backend/tests/unit/test_vp_access_code_sync_migration.py",
        "backend/tests/unit/test_vp_mode_rename_migration.py",
    }
)

CREATE_ALL_ALLOWLIST = frozenset(
    CREATE_ASYNC_ENGINE_ALLOWLIST
    - {
        # These three build an engine but never call create_all.
        "backend/tests/integration/test_security.py",
        "backend/tests/unit/test_db_dialect.py",
        "backend/tests/unit/test_user_print_template_rename_migration.py",
    }
)

ASYNC_SESSIONMAKER_ALLOWLIST = frozenset(
    {
        "backend/tests/integration/test_capability_gate_api.py",
        "backend/tests/integration/test_print_lifecycle.py",
        "backend/tests/integration/test_queue_start_user_attribution.py",
        "backend/tests/integration/test_security.py",
        "backend/tests/integration/test_spoolman_tracking_slot_fallback.py",
        "backend/tests/unit/services/test_ams_presence.py",
        "backend/tests/unit/services/test_farm_policy.py",
        "backend/tests/unit/services/test_farm_staging.py",
        "backend/tests/unit/services/test_farm_stall.py",
        "backend/tests/unit/services/test_hms_event.py",
        "backend/tests/unit/services/test_incident_wire_sweep.py",
        "backend/tests/unit/services/test_pause_recovery.py",
        "backend/tests/unit/services/test_printer_incidents.py",
        "backend/tests/unit/services/test_queue_transitions.py",
        "backend/tests/unit/services/test_release_liveness.py",
        "backend/tests/unit/services/test_runout_release_replay.py",
        "backend/tests/unit/services/test_scheduler_hold_unpin.py",
        "backend/tests/unit/services/test_scheduler_library_file_missing.py",
        "backend/tests/unit/services/test_scheduler_parallel_dispatch.py",
        "backend/tests/unit/services/test_service_hold.py",
        "backend/tests/unit/services/test_slot_pipeline.py",
        "backend/tests/unit/services/test_spool_recovery.py",
        "backend/tests/unit/services/test_spool_respool.py",
        "backend/tests/unit/services/test_spool_tagless.py",
        "backend/tests/unit/services/test_stagger.py",
        "backend/tests/unit/services/test_usb_storage.py",
        "backend/tests/unit/services/test_user_deletion.py",
        "backend/tests/unit/test_cli.py",
        "backend/tests/unit/test_ldap_migration.py",
        "backend/tests/unit/test_main_hms_pipeline.py",
        "backend/tests/unit/test_migrations_spool_lifecycle.py",
        "backend/tests/unit/test_phantom_print_hardening.py",
        "backend/tests/unit/test_print_log_backfill_migration.py",
        "backend/tests/unit/test_runtime_tracking_pause.py",
        "backend/tests/unit/test_scheduler_cleanup_library.py",
        "backend/tests/unit/test_scheduler_clear_plate.py",
        "backend/tests/unit/test_scheduler_filament_deficit.py",
        "backend/tests/unit/test_scheduler_pin_contract.py",
        "backend/tests/unit/test_scheduler_skip_machine_code.py",
        "backend/tests/unit/test_scheduler_stagger.py",
        "backend/tests/unit/test_scheduler_watchdog.py",
    }
)

CLOCK_CLASS_ALLOWLIST = frozenset(
    {
        "backend/tests/unit/services/eject/test_remote.py",
        "backend/tests/unit/services/test_ams_write_epoch.py",
        "backend/tests/unit/services/test_plate_occupancy.py",
        "backend/tests/unit/services/test_spool_recovery.py",
        "backend/tests/unit/services/test_spool_tagless_reconcile.py",
        "backend/tests/unit/test_retry_window.py",
    }
)

# Assignment to a directory attribute of the LIVE settings singleton. Unlike
# ``monkeypatch.setattr(settings, "archive_dir", ...)``, a bare assignment is
# never undone, so it leaks into every later test in the worker.
SETTINGS_DIR_ASSIGNMENT_ALLOWLIST = frozenset(
    {
        # :103-104 stashes and :140 restores base_dir by hand; it survives a
        # passing test but not a failure before the restore.
    }
)


@pytest.fixture(scope="module")
def test_tree() -> ParsedTree:
    """Every module under ``backend/tests``, parsed once for this file."""
    return ParsedTree(root=TESTS_ROOT)


def _scannable(tree: ParsedTree) -> tuple[ParsedModule, ...]:
    """Every test module except the owner package itself."""
    return tuple(m for m in tree.modules() if not m.posix_rel_to_repo.startswith(OWNER_PREFIX))


def _calls_named(module: ParsedModule, name: str) -> bool:
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            return True
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
    return False


def _touches_metadata_create_all(module: ParsedModule) -> bool:
    # `conn.run_sync(Base.metadata.create_all)` passes the bound method rather
    # than calling it, so match the attribute access, not a call.
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Attribute) and node.attr == "create_all":
            if ast.unparse(node.value).endswith("metadata"):
                return True
    return False


def _defines_clock_class(module: ParsedModule) -> bool:
    return any(isinstance(node, ast.ClassDef) and node.name.endswith("Clock") for node in ast.walk(module.tree))


def _settings_aliases(module: ParsedModule) -> set[str]:
    """Names bound to the live ``settings`` singleton in this module."""
    aliases: set[str] = set()
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom) and node.module == "backend.app.core.config":
            for alias in node.names:
                if alias.name == "settings":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _assigns_settings_dir(module: ParsedModule) -> list[str]:
    aliases = _settings_aliases(module)
    if not aliases:
        return []
    offences: list[str] = []
    for node in ast.walk(module.tree):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr.endswith("_dir")
                and isinstance(target.value, ast.Name)
                and target.value.id in aliases
            ):
                offences.append(f"{module.posix_rel_to_repo}:{node.lineno}: {ast.unparse(node)}")
    return offences


def _assert_ratchet(offenders: set[str], allowlist: frozenset[str], what: str, owner: str) -> None:
    new = sorted(offenders - allowlist)
    assert not new, (
        f"{len(new)} file(s) define their own {what} instead of using {owner}:\n  "
        + "\n  ".join(new)
        + f"\n\nImport it from backend/tests/_fixtures/ rather than adding these to the "
        f"allowlist in {Path(__file__).name} — the allowlist only shrinks."
    )
    stale = sorted(allowlist - offenders)
    assert not stale, (
        f"{len(stale)} file(s) no longer define their own {what} — delete them from the "
        f"allowlist in {Path(__file__).name}:\n  " + "\n  ".join(stale)
    )


def test_only_fixtures_build_the_engine(test_tree: ParsedTree) -> None:
    """``create_async_engine`` belongs to ``_fixtures/db.py``."""
    offenders = {m.posix_rel_to_repo for m in _scannable(test_tree) if _calls_named(m, "create_async_engine")}
    _assert_ratchet(
        offenders,
        CREATE_ASYNC_ENGINE_ALLOWLIST,
        "async engine",
        "_fixtures.db.create_memory_engine()",
    )


def test_only_fixtures_create_the_schema(test_tree: ParsedTree) -> None:
    """``Base.metadata.create_all`` belongs to ``_fixtures/db.py``."""
    offenders = {m.posix_rel_to_repo for m in _scannable(test_tree) if _touches_metadata_create_all(m)}
    _assert_ratchet(
        offenders,
        CREATE_ALL_ALLOWLIST,
        "schema build",
        "_fixtures.db.create_memory_engine()",
    )


def test_only_fixtures_build_session_factories(test_tree: ParsedTree) -> None:
    """``async_sessionmaker`` belongs to ``_fixtures/db.py``."""
    offenders = {m.posix_rel_to_repo for m in _scannable(test_tree) if _calls_named(m, "async_sessionmaker")}
    _assert_ratchet(
        offenders,
        ASYNC_SESSIONMAKER_ALLOWLIST,
        "session factory",
        "the db_session / own_session_factory fixtures",
    )


def test_only_fixtures_define_a_clock(test_tree: ParsedTree) -> None:
    """A fake clock belongs to ``_fixtures/clock.py``."""
    offenders = {m.posix_rel_to_repo for m in _scannable(test_tree) if _defines_clock_class(m)}
    _assert_ratchet(offenders, CLOCK_CLASS_ALLOWLIST, "fake clock class", "_fixtures.clock.FakeClock")


def test_nothing_assigns_a_directory_onto_the_live_settings(test_tree: ParsedTree) -> None:
    """The data root is owned by ``core/paths.py``; tests never repoint it by assignment.

    ``monkeypatch.setattr(settings, "...", ...)`` is the sanctioned escape hatch —
    it is undone at teardown. A bare assignment is not, which is how a temp
    directory from one file became another file's archive root.
    """
    offences: list[str] = []
    offenders: set[str] = set()
    for module in _scannable(test_tree):
        found = _assigns_settings_dir(module)
        if found:
            offences.extend(found)
            offenders.add(module.posix_rel_to_repo)

    new = sorted(o for o in offences if o.split(":", 1)[0] not in SETTINGS_DIR_ASSIGNMENT_ALLOWLIST)
    assert not new, (
        "settings directory attributes are assigned (and never restored) at:\n  "
        + "\n  ".join(new)
        + "\n\nUse monkeypatch.setattr(settings, ...) instead."
    )
    stale = sorted(SETTINGS_DIR_ASSIGNMENT_ALLOWLIST - offenders)
    assert not stale, (
        f"{len(stale)} file(s) no longer assign a settings directory — delete them from the "
        f"allowlist in {Path(__file__).name}:\n  " + "\n  ".join(stale)
    )
