"""The id-reuse CENSUS: every mapped table has a decided answer to "may its ids recycle?".

005-H2S (2026-09-17). SQLite recycles the rowid of a deleted MAX row unless the table is
declared AUTOINCREMENT, and this fork deliberately leaves FK enforcement off — so a
reference to a purged row does not fail, it silently RE-BINDS to whatever row next takes
that id. ``library_files`` id 109 was purged and re-issued twice on 2026-09-16, a completed
unit's ``library_file_id`` re-pointed at a stranger file, and its eject built the wrong
container.

The fix is only durable if the NEXT deletable table is classified too, which is what this
file is for. Adding a mapped table without deciding its answer breaks CI with a decision to
make — the same shape as ``services/test_requeue_fields.py``'s union census, and for the
same reason: "nobody thought about it" must stop being one of the available answers.

The reasons are not prose. ``_NO_INBOUND`` is re-derived from the live metadata below, so a
new foreign key pointing at a table filed under it fails this census rather than quietly
turning a true reason false.
"""

from __future__ import annotations

import collections

from backend.app.core.database import _AUTOINCREMENT_TABLES

# ── the classification vocabulary ──────────────────────────────────────────────────────
# No row anywhere holds this table's ids, so a recycled id has nothing to mis-bind to.
# CHECKED against the metadata by ``test_the_no_inbound_reasons_are_still_true``.
_NO_INBOUND = "no inbound FK — "
# Referenced, but the delete path resolves every referencing row in the same operation, so
# no reference outlives the row. (``ondelete=`` is inert with FK enforcement off; what does
# the work is the ORM relationship cascade or explicit cleanup code.)
_CLEARED = "referenced, cleared on delete — "

# ── the forbidden set (D6, closed 2026-09-17) ──────────────────────────────────────────
# One origin: the tuple the migration itself walks. A table that stops being rebuilt stops
# being claimed safe here in the same edit.
#
# There is deliberately no "deferred" third bucket. The first cut of this census had one —
# five tables that were referenced AND left dangling but sat outside the wave's original
# scope — and a bucket for "unsafe, but not today" is how a closed class reopens. The
# operator ruling was to close it here, so every one of them is in the forbidden set and
# the only remaining answers are "cannot mis-bind" and "must never reuse an id".
ID_REUSE_FORBIDDEN = frozenset(_AUTOINCREMENT_TABLES)

# ── everything else, one line of reasoning each ────────────────────────────────────────
ID_REUSE_REASONED: dict[str, str] = {
    # --- no inbound foreign key at all -------------------------------------------------
    "active_print_spoolman": _NO_INBOUND + "per-print scratch row, nothing cites it",
    "ams_labels": _NO_INBOUND + "printable label rows, a leaf",
    "ams_sensor_history": _NO_INBOUND + "append-only sample history, a leaf",
    "api_keys": _NO_INBOUND + "operator-deletable, but no row stores a key's id",
    "auth_ephemeral_tokens": _NO_INBOUND + "short-lived, self-purging, a leaf",
    "auth_rate_limit_events": _NO_INBOUND + "append-only attempt log, a leaf",
    "bug_reports": _NO_INBOUND + "report rows are a leaf",
    "color_catalog": _NO_INBOUND + "seeded catalog matched by value, never by id",
    "external_links": _NO_INBOUND + "UI link rows are a leaf",
    "farm_cycle_episode": _NO_INBOUND + "append-only cooldown/eject measurements, a leaf",
    "filament_shopping_list": _NO_INBOUND + "procurement rows are a leaf",
    "filament_sku_settings": _NO_INBOUND + "per-filament settings, a leaf",
    "filaments": _NO_INBOUND + "filament definitions are referenced by value, not id",
    "github_backup_logs": _NO_INBOUND + "append-only backup log, a leaf",
    "hms_event": _NO_INBOUND + "per-(printer, code) vocabulary row, a leaf",
    "kprofile_notes": _NO_INBOUND + "operator notes, a leaf",
    "library_file_tags": _NO_INBOUND + "association table — composite PK, no surrogate id",
    "local_presets": _NO_INBOUND + "slicer preset rows are a leaf",
    "long_lived_tokens": _NO_INBOUND + "token rows are a leaf",
    "maintenance_history": _NO_INBOUND + "append-only service history, a leaf",
    "notification_digest_queue": _NO_INBOUND + "transient digest spool, a leaf",
    "notification_ledger": _NO_INBOUND + "composite PK (scope, dedup_key) — no integer id exists",
    "notification_logs": _NO_INBOUND + "append-only send log, a leaf",
    "notification_templates": _NO_INBOUND + "seeded per event_type, matched by name",
    "orca_base_profiles": _NO_INBOUND + "cached vendor profiles, a leaf",
    "pending_uploads": _NO_INBOUND + "in-flight upload rows are a leaf",
    "print_log_entries": _NO_INBOUND + "append-only print log, a leaf",
    "printer_incident": _NO_INBOUND + "durable fault rows are a leaf",
    "printer_model_geometry": _NO_INBOUND + "registry keyed by model_key in every reader",
    "printer_observation_span": _NO_INBOUND + "append-only observation history, a leaf",
    "printer_sensor_history": _NO_INBOUND + "append-only sample history, a leaf",
    "project_bom_items": _NO_INBOUND + "BOM line items are a leaf",
    "recovery_escalation": _NO_INBOUND + "durable escalation rows are a leaf",
    "settings": _NO_INBOUND + "key/value rows read by key, never by id",
    "slot_preset_mappings": _NO_INBOUND + "per-slot preset mapping, a leaf",
    "slot_recheck_intent": _NO_INBOUND + "operator re-check intents are a leaf",
    "smart_plug_energy_snapshots": _NO_INBOUND + "append-only energy samples, a leaf",
    "spool_assignment": _NO_INBOUND + "spool<->slot binding rows are a leaf",
    "spool_catalog": _NO_INBOUND + "seeded catalog matched by value, never by id",
    "spool_k_profile": _NO_INBOUND + "per-spool calibration rows are a leaf",
    "spool_usage_history": _NO_INBOUND + "append-only gram ledger, a leaf",
    "spoolbuddy_devices": _NO_INBOUND + "device rows are a leaf",
    "spoolman_k_profile": _NO_INBOUND + "Spoolman-mode calibration rows are a leaf",
    "spoolman_slot_assignments": _NO_INBOUND + "Spoolman-mode binding rows are a leaf",
    "user_email_preferences": _NO_INBOUND + "per-user preference rows are a leaf",
    "user_groups": _NO_INBOUND + "association table — composite PK, no surrogate id",
    "user_oidc_links": _NO_INBOUND + "per-user SSO link rows are a leaf",
    "user_otp_codes": _NO_INBOUND + "short-lived OTP rows are a leaf",
    "user_totp": _NO_INBOUND + "per-user TOTP secret rows are a leaf",
    "virtual_printers": _NO_INBOUND + "dev/test printer rows are a leaf",
    # --- referenced, but nothing survives the delete ------------------------------------
    "github_backup_config": _CLEARED + "ORM cascade deletes github_backup_logs with the config",
    "library_folders": _CLEARED + "library_trash detaches member files, then ORM-cascades child folders",
    "library_tags": _CLEARED + "the file<->tag association rows go with the tag (M2M secondary)",
    "locations": _CLEARED + "deleting a location nullifies spool.location_id for every child",
    "maintenance_types": _CLEARED + "system types soft-delete; a custom type ORM-cascades its items and history",
    "notification_providers": _CLEARED + "ORM cascade takes notification_logs and the digest queue",
    "oidc_providers": _CLEARED + "ORM cascade deletes user_oidc_links with the provider",
    "printer_maintenance": _CLEARED + "ORM cascade deletes maintenance_history with the item",
}


def _all_tables() -> set[str]:
    """Every mapped table, registered the way a real boot registers them."""
    import backend.app.models  # noqa: F401
    from backend.app.core.database import Base
    from backend.app.models import (  # noqa: F401
        active_print_spoolman,
        bug_report,
        external_link,
        filament_sku_settings,
        print_log,
        print_queue,
        project_bom,
        shopping_list,
        slot_preset,
        spoolman_k_profile,
        spoolman_slot_assignment,
        virtual_printer,
    )

    return set(Base.metadata.tables)


def _inbound_references() -> dict[str, set[str]]:
    """table -> the tables holding a foreign key to it (self-references included)."""
    import backend.app.models  # noqa: F401
    from backend.app.core.database import Base

    _all_tables()
    inbound: dict[str, set[str]] = collections.defaultdict(set)
    for name, table in Base.metadata.tables.items():
        for fk in table.foreign_keys:
            inbound[fk.column.table.name].add(name)
    return inbound


class TestCensus:
    def test_every_mapped_table_is_classified(self):
        """The union IS the mapped table set — the pin that forces a decision.

        A new table joins ID_REUSE_FORBIDDEN (operator-deletable, and its ids outlive it in
        other rows — so it needs ``sqlite_autoincrement`` AND a place in
        ``core.database._AUTOINCREMENT_TABLES``) or the reasoned remainder, with the reason
        written down. There is no third answer.
        """
        tables = _all_tables()
        union = ID_REUSE_FORBIDDEN | set(ID_REUSE_REASONED)
        assert not union - tables, f"the census names a table that is not mapped: {sorted(union - tables)}"
        assert not tables - union, f"a mapped table has no id-reuse decision: {sorted(tables - union)}"

    def test_the_two_sets_are_disjoint(self):
        assert not ID_REUSE_FORBIDDEN & set(ID_REUSE_REASONED)

    def test_every_forbidden_table_carries_sqlite_autoincrement(self):
        """The flag is what ``create_all`` gives a fresh install; the migration retrofits the rest."""
        from backend.app.core.database import Base

        _all_tables()
        for name in sorted(ID_REUSE_FORBIDDEN):
            table = Base.metadata.tables[name]
            assert table.dialect_options["sqlite"]["autoincrement"] is True, (
                f"{name} is in ID_REUSE_FORBIDDEN but its model does not set sqlite_autoincrement"
            )

    def test_no_reasoned_table_carries_sqlite_autoincrement(self):
        """The partition is real in the schema too, not only in this file."""
        from backend.app.core.database import Base

        _all_tables()
        for name in sorted(ID_REUSE_REASONED):
            table = Base.metadata.tables[name]
            assert table.dialect_options["sqlite"]["autoincrement"] is False, (
                f"{name} sets sqlite_autoincrement but is filed in the reasoned remainder — "
                "move it into core.database._AUTOINCREMENT_TABLES so the migration rebuilds it too"
            )

    def test_the_no_inbound_reasons_are_still_true(self):
        """``_NO_INBOUND`` is a fact about the schema, so it is re-derived, not trusted.

        A new foreign key pointing at one of these tables turns its stated reason false. This
        case is what makes the census self-maintaining instead of a comment that rots.
        """
        inbound = _inbound_references()
        wrong = {
            name: sorted(inbound[name])
            for name, reason in ID_REUSE_REASONED.items()
            if reason.startswith(_NO_INBOUND) and inbound.get(name)
        }
        assert not wrong, (
            f"these tables are filed as unreferenced but now have inbound foreign keys: {wrong} — "
            "re-decide them (cleared on delete, or forbidden)"
        )

    def test_every_referenced_table_is_reasoned_beyond_no_inbound(self):
        """The converse: a table other rows DO cite may not hide behind the leaf reason."""
        inbound = _inbound_references()
        referenced = {name for name, sources in inbound.items() if sources}
        for name in sorted(referenced & set(ID_REUSE_REASONED)):
            assert not ID_REUSE_REASONED[name].startswith(_NO_INBOUND), name

    def test_the_forbidden_set_is_exactly_the_migration_list(self):
        """One origin. The census cannot drift from what ``run_migrations`` actually rebuilds."""
        assert frozenset(_AUTOINCREMENT_TABLES) == ID_REUSE_FORBIDDEN
        assert len(_AUTOINCREMENT_TABLES) == len(set(_AUTOINCREMENT_TABLES)) == 13

    def test_the_whole_dangling_reference_class_is_forbidden(self):
        """The five non-farm tables the delete-path audit found are IN, not scheduled.

        Each leaves at least one reference standing after the row is gone — the exact shape
        that re-bound ``library_file_id`` 109. They are named here so a later edit that
        quietly drops one from ``_AUTOINCREMENT_TABLES`` fails with the reason attached.
        """
        assert {"users", "groups", "projects", "smart_plugs", "spool"} <= ID_REUSE_FORBIDDEN
