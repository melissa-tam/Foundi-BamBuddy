import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled, caller_is_api_key, require_energy_cost_update
from backend.app.core.config import APP_VERSION, BUILD_VERSION, settings as app_settings
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])

DEFAULT_SETTINGS = AppSettings()

# Sensitive credential fields blanked for API-key callers
_SENSITIVE_FIELDS_FOR_API_KEY = (
    "mqtt_password",
    "ha_token",
    "prometheus_token",
    "virtual_printer_access_code",
    "ldap_bind_password",
    "erp_db_password",
)


# Top-level ZIP member recording what a backup actually contains. A backup ZIP
# is produced even when individual files could not be copied, so completeness is
# a fact the artifact has to carry with it — restore wipes the destination and
# cannot re-derive it afterwards.
BACKUP_MANIFEST_NAME = "backup_manifest.json"
BACKUP_MANIFEST_VERSION = 1


def _describe_copy_failure(name: str, exc: OSError) -> list[dict[str, str]]:
    """Flatten one directory-copy failure into per-path manifest records.

    ``shutil.copytree`` accumulates per-file failures and raises a single
    ``shutil.Error`` carrying an ``(src, dst, why)`` triple per failed file, so
    the manifest can name the affected paths instead of one opaque string.
    """
    import shutil

    if isinstance(exc, shutil.Error) and exc.args and isinstance(exc.args[0], list):
        records: list[dict[str, str]] = []
        for entry in exc.args[0]:
            if isinstance(entry, (tuple, list)) and len(entry) >= 3:
                records.append({"dir": name, "path": str(entry[0]), "error": str(entry[2])})
            else:
                records.append({"dir": name, "path": "", "error": str(entry)})
        if records:
            return records
    return [{"dir": name, "path": str(getattr(exc, "filename", "") or ""), "error": str(exc)}]


def _parse_manifest(raw: bytes) -> dict | None:
    """Parse manifest bytes; ``None`` means unreadable — never means 'clean'."""
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_manifest_from_zip(zip_path: Path) -> dict | None:
    """Read the manifest back out of a produced backup ZIP (absent → None)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = zf.read(BACKUP_MANIFEST_NAME)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    return _parse_manifest(raw)


def _manifest_partial_dirs(manifest: dict | None) -> list[str]:
    """Top-level directory names the manifest recorded copy failures for."""
    failures = (manifest or {}).get("failures") or []
    if not isinstance(failures, list):
        return []
    names = {str(f.get("dir")) for f in failures if isinstance(f, dict) and f.get("dir")}
    return sorted(names)


def _partial_refusal_reason(manifest_present: bool, manifest: dict | None) -> str | None:
    """Why a restore of this backup must be refused, or None if it may proceed.

    A backup ZIP written before manifests existed is allowed (the caller notes
    the absence); a manifest that exists but cannot be trusted is not.
    """
    if not manifest_present:
        return None
    if manifest is None:
        return "This backup's manifest is unreadable, so its completeness cannot be verified."
    if not manifest.get("partial"):
        return None
    dirs = _manifest_partial_dirs(manifest)
    where = f" Incomplete directories: {', '.join(dirs)}." if dirs else ""
    return (
        "This backup was recorded as INCOMPLETE when it was created — some files could not be copied."
        f"{where} Restoring it would replace the current data with a known-partial copy."
    )


def _move_dir_contents(dest_dir: Path, aside_dir: Path) -> None:
    """Move every child of ``dest_dir`` into a fresh ``aside_dir`` (same volume).

    ``dest_dir`` itself is never renamed: on Docker installs it can be a bind
    mount point. A failure part-way through moves the already-moved children
    back, so the caller can treat a raise as "nothing was destroyed".
    """
    aside_dir.mkdir(parents=True, exist_ok=False)
    moved: list[str] = []
    try:
        for item in dest_dir.iterdir():
            os.replace(item, aside_dir / item.name)
            moved.append(item.name)
    except OSError:
        for child in moved:
            try:
                os.replace(
                    aside_dir / child, dest_dir / child
                )  # SEC-PATH-OK: child is a name this function itself read from dest_dir.iterdir() and moved into aside_dir
            except OSError:
                logger.error("Could not move %s back into %s during abort", child, dest_dir, exc_info=True)
        try:
            aside_dir.rmdir()
        except OSError:
            pass
        raise


def _rollback_restored_dirs(moved_aside: list[tuple[str, Path, Path]]) -> str:
    """Undo a failed directory restore; returns an operator-facing note.

    Drains ``moved_aside`` (last moved first), so calling it again from an
    outer handler is a no-op rather than a second, destructive pass.
    """
    import shutil

    unrecovered: list[str] = []
    rolled_back = False
    while moved_aside:
        name, dest_dir, aside_dir = moved_aside.pop()
        rolled_back = True
        try:
            for item in dest_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in aside_dir.iterdir():
                os.replace(item, dest_dir / item.name)
            aside_dir.rmdir()
        except OSError:
            logger.error("Rollback of %s failed; originals kept at %s", name, aside_dir, exc_info=True)
            unrecovered.append(f"{name} (originals kept at {aside_dir})")
    if unrecovered:
        return "Rollback INCOMPLETE for: " + "; ".join(unrecovered) + "."
    if not rolled_back:
        return ""
    return "All data directories were rolled back to their pre-restore state."


def _discard_aside_dirs(moved_aside: list[tuple[str, Path, Path]]) -> str:
    """Delete the pre-restore copies — the restore's commit point.

    Drains ``moved_aside`` so a later failure can no longer roll back what has
    already been committed.
    """
    import shutil

    leftover: list[str] = []
    while moved_aside:
        _name, _dest_dir, aside_dir = moved_aside.pop()
        try:
            shutil.rmtree(aside_dir)
        except OSError:
            logger.warning("Could not remove pre-restore copy %s", aside_dir, exc_info=True)
            leftover.append(str(aside_dir))
    if leftover:
        return "Note: pre-restore copies could not be deleted and still occupy disk: " + ", ".join(leftover) + "."
    return ""


def _sqlalchemy_type_to_sqlite_type(type_repr: str) -> str:
    """Map a SQLAlchemy column type's ``str()`` to a SQLite-native column type.

    Used by ``create_backup_zip`` to reconstruct a portable SQLite database
    file from PostgreSQL data. Falling through to TEXT for binary columns
    corrupts non-UTF8 bytes — the BLOB branch is the #1333 regression guard
    for OIDC icon BLOBs.

    Extracted as a pure helper so it can be unit-tested without spinning up
    the full FastAPI app + backup pipeline.
    """
    type_str = type_repr.upper()
    if "INT" in type_str:
        return "INTEGER"
    if "FLOAT" in type_str or "REAL" in type_str or "NUMERIC" in type_str:
        return "REAL"
    if "BOOL" in type_str:
        return "BOOLEAN"
    if "BLOB" in type_str or "BYTEA" in type_str or "BINARY" in type_str:
        # OIDC icon BLOB column (#1333) — without this branch the column
        # was created as TEXT and non-UTF8 bytes were corrupted during the
        # PG→SQLite-ZIP backup round trip.
        return "BLOB"
    return "TEXT"


async def get_setting(db: AsyncSession, key: str) -> str | None:
    """Get a single setting value by key."""
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def get_external_login_url(db: AsyncSession) -> str:
    """Get the external URL for the login page.

    Uses external_url from settings if available, otherwise falls back to APP_URL env var.

    Args:
        db: Database session

    Returns:
        Full URL to the login page
    """
    import os

    external_url = await get_setting(db, "external_url")
    if external_url:
        external_url = external_url.rstrip("/")
    else:
        external_url = os.environ.get("APP_URL", "http://localhost:5173")
    return external_url + "/login"


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    """Set a single setting value."""
    from backend.app.core.db_dialect import upsert_setting

    await upsert_setting(db, Settings, key, value)


async def _build_settings_response(db: AsyncSession, is_api_key: bool = False) -> AppSettings:
    """Build the full settings response, scrubbing secrets for API-key callers."""
    settings_dict = DEFAULT_SETTINGS.model_dump()

    result = await db.execute(select(Settings))
    for setting in result.scalars().all():
        if setting.key not in settings_dict:
            continue
        if setting.key in [
            "auto_archive",
            "save_thumbnails",
            "capture_finish_photo",
            "spoolman_enabled",
            "spoolman_disable_weight_sync",
            "spoolman_report_partial_usage",
            "auto_add_unknown_rfid",
            "disable_filament_warnings",
            "spool_recovery_enabled",
            "runout_auto_resume_enabled",
            "check_updates",
            "check_printer_firmware",
            "include_beta_updates",
            "virtual_printer_enabled",
            "ftp_retry_enabled",
            "mqtt_enabled",
            "mqtt_use_tls",
            "ha_enabled",
            "per_printer_mapping_expanded",
            "prometheus_enabled",
            "user_notifications_enabled",
            "queue_drying_enabled",
            "queue_drying_block",
            "ambient_drying_enabled",
            "print_drying_enabled",
            "require_plate_clear",
            "farm_usb_auto_cleanup",
            "farm_idle_park_enabled",
            "queue_shortest_first",
            "default_bed_levelling",
            "default_flow_cali",
            "default_vibration_cali",
            "default_layer_inspect",
            "default_timelapse",
            "default_nozzle_offset_cali",
            "ldap_enabled",
            "ldap_auto_provision",
            "local_login_enabled",
            "erp_db_ssl",
            "stagger_dynamic_release",
        ]:
            settings_dict[setting.key] = setting.value.lower() == "true"
        elif setting.key in [
            "default_filament_cost",
            "energy_cost_per_kwh",
            "ams_temp_good",
            "ams_temp_fair",
            "library_disk_warning_gb",
            "low_stock_threshold",
            "farm_cooldown_stall_epsilon_c",
            "farm_cooldown_plateau_eject_margin_c",
            "dispatch_kick_debounce_seconds",
            "usb_preflight_max_wait_seconds",
            "stagger_release_epsilon_c",
        ]:
            settings_dict[setting.key] = float(setting.value)
        elif setting.key in [
            "ams_humidity_good",
            "ams_humidity_fair",
            "ams_history_retention_days",
            "printer_sensor_history_retention_days",
            "ftp_retry_count",
            "ftp_retry_delay",
            "ftp_timeout",
            "mqtt_port",
            "stagger_group_size",
            "stagger_interval_minutes",
            "stagger_heatup_grace_seconds",
            "queue_check_interval_seconds",
            "usb_preflight_fresh_window_seconds",
            "dispatch_parallel_limit",
            "forecast_global_lead_time_days",
            "session_max_hours",
            "min_start_spool_g",
            "spool_recovery_max_attempts",
            "spool_recovery_step_timeout_s",
            "spool_recovery_protect_layers",
            "farm_retry_max_per_unit",
            "farm_escalate_consecutive_failures",
            "farm_cooldown_warn_floor_c",
            "farm_offline_stall_minutes",
            "farm_pause_stall_minutes",
            "farm_cooldown_stall_window_minutes",
            "farm_cooldown_max_hold_minutes",
            "farm_idle_park_percent",
            "respool_prompt_threshold_g",
            "erp_db_port",
        ]:
            settings_dict[setting.key] = int(setting.value)
        elif setting.key == "default_printer_id":
            settings_dict[setting.key] = int(setting.value) if setting.value and setting.value != "None" else None
        elif setting.key == "open_in_slicer":
            # None means "inherit from preferred_slicer" (#1329). The PUT path
            # serializes None as the literal string "None"; strip it back so
            # the frontend sees a true null and falls back as intended.
            settings_dict[setting.key] = setting.value if setting.value and setting.value != "None" else None
        else:
            settings_dict[setting.key] = setting.value

    ha_settings = await get_homeassistant_settings(db)
    settings_dict.update(ha_settings)

    # ldap_bind_password is never returned to any caller
    settings_dict["ldap_bind_password"] = ""
    # erp_db_password is never returned to any caller
    settings_dict["erp_db_password"] = ""
    # erp_login_active is DERIVED (read-only): true when ERP connection config
    # resolves from the deploy erp.env file or DB overrides. Computed via the
    # single resolver so the status matches exactly what the login path uses.
    from backend.app.services.erp_directory import parse_erp_config, resolve_erp_settings

    settings_dict["erp_login_active"] = parse_erp_config(await resolve_erp_settings(db)) is not None

    if is_api_key:
        for field in _SENSITIVE_FIELDS_FOR_API_KEY:
            if field in settings_dict:
                settings_dict[field] = ""

    return AppSettings(**settings_dict)


@router.get("", response_model=AppSettings)
@router.get("/", response_model=AppSettings)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
    _is_api_key: bool = Depends(caller_is_api_key),
):
    """Get all application settings."""
    return await _build_settings_response(db, is_api_key=_is_api_key)


@router.put("/", response_model=AppSettings)
async def update_settings(
    settings_update: AppSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Update application settings."""
    update_data = settings_update.model_dump(exclude_unset=True)

    # Safety refusals on disabling local login (#1589). Two failure modes
    # would otherwise lock everyone out of the install:
    #   1. No enabled OIDC provider exists — nobody could authenticate.
    #   2. The caller has no UserOIDCLink — they would lock themselves out
    #      even if other admins are linked.
    # Either case returns HTTP 400 instead of silently saving. The
    # ``BAMBUDDY_LOCAL_LOGIN=true`` env-var bypass on /auth/login is a
    # separate recovery path; the refusals here protect the *default*
    # configuration where the env var is absent.
    if update_data.get("local_login_enabled") is False:
        from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink

        enabled_count = await db.scalar(select(func.count(OIDCProvider.id)).where(OIDCProvider.is_enabled.is_(True)))
        if not enabled_count:
            raise HTTPException(
                status_code=400,
                detail="Cannot disable local login: no OIDC provider is enabled.",
            )
        if current_user is not None:
            caller_links = await db.scalar(
                select(func.count(UserOIDCLink.id)).where(UserOIDCLink.user_id == current_user.id)
            )
            if not caller_links:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot disable local login: your account has no OIDC link, so you would lock yourself out.",
                )

    # Check if any MQTT settings are being updated
    mqtt_keys = {
        "mqtt_enabled",
        "mqtt_broker",
        "mqtt_port",
        "mqtt_username",
        "mqtt_password",
        "mqtt_topic_prefix",
        "mqtt_use_tls",
    }
    mqtt_updated = bool(mqtt_keys & set(update_data.keys()))

    for key, value in update_data.items():
        # Convert value to string for storage
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        elif value is None:
            str_value = "None"
        else:
            str_value = str(value)
        await set_setting(db, key, str_value)

    await db.commit()
    # Expire all objects to ensure fresh reads after commit
    db.expire_all()

    # Reconfigure MQTT relay if any MQTT settings changed
    if mqtt_updated:
        try:
            from backend.app.services.mqtt_relay import mqtt_relay

            mqtt_settings = {
                "mqtt_enabled": (await get_setting(db, "mqtt_enabled") or "false") == "true",
                "mqtt_broker": await get_setting(db, "mqtt_broker") or "",
                "mqtt_port": int(await get_setting(db, "mqtt_port") or "1883"),
                "mqtt_username": await get_setting(db, "mqtt_username") or "",
                "mqtt_password": await get_setting(db, "mqtt_password") or "",
                "mqtt_topic_prefix": await get_setting(db, "mqtt_topic_prefix") or "bambuddy",
                "mqtt_use_tls": (await get_setting(db, "mqtt_use_tls") or "false") == "true",
            }
            await mqtt_relay.configure(mqtt_settings)
        except Exception:
            pass  # Don't fail the settings update if MQTT reconfiguration fails

    # Return updated settings (never scrub secrets on PUT — caller has SETTINGS_UPDATE permission)
    return await _build_settings_response(db, is_api_key=False)


@router.patch("/", response_model=AppSettings)
@router.patch("", response_model=AppSettings)
async def patch_settings(
    settings_update: AppSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Partially update application settings (same as PUT, for REST compatibility)."""
    return await update_settings(settings_update, db, _)


class ElectricityPriceUpdate(BaseModel):
    """Payload for ``POST /settings/electricity-price`` (#1356).

    Mirrors the field name documented in ``wiki/features/energy.md`` so the
    Home Assistant ``rest_command`` example needs only a URL change, not a
    payload change. Plain non-negative float; tariffs can go as low as 0.0 in
    some markets (e.g. free hours).
    """

    energy_cost_per_kwh: float = Field(ge=0)


@router.post("/electricity-price", response_model=AppSettings)
async def update_electricity_price(
    payload: ElectricityPriceUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_energy_cost_update()),
    _is_api_key: bool = Depends(caller_is_api_key),
):
    """Update the per-kWh electricity cost used by the energy-tracking pipeline.

    This is the only settings field writable via API key, gated by the
    ``can_update_energy_cost`` toggle on the key. JWT users still need the
    standard ``SETTINGS_UPDATE`` permission. See #1356 for the rationale —
    the general ``PATCH /settings`` route remains denied for API keys because
    it can rewrite SMTP/LDAP/MQTT credentials, which is a much wider surface
    than the documented dynamic-tariff use case requires.
    """
    await set_setting(db, "energy_cost_per_kwh", str(payload.energy_cost_per_kwh))
    await db.commit()
    db.expire_all()
    return await _build_settings_response(db, is_api_key=_is_api_key)


@router.post("/reset", response_model=AppSettings)
async def reset_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Reset all settings to defaults."""
    # Delete all settings
    result = await db.execute(select(Settings))
    for setting in result.scalars().all():
        await db.delete(setting)

    await db.commit()

    return DEFAULT_SETTINGS


@router.get("/default-sidebar-order")
async def get_default_sidebar_order(
    db: AsyncSession = Depends(get_db),
):
    """Get the admin-set default sidebar order.

    Intentionally unauthenticated: non-admin users need to read this value to apply
    the default sidebar order, but may lack SETTINGS_READ permission.
    The value is non-sensitive (sidebar item IDs only).
    """
    value = await get_setting(db, "default_sidebar_order")
    return {"default_sidebar_order": value or ""}


# Fields exposed via /ui-preferences without SETTINGS_READ. Each entry MUST be
# non-sensitive (no credentials, no PII, no secret tokens) — granting SETTINGS_READ
# also grants visibility of SMTP/LDAP/MQTT passwords and similar, so the goal of
# this endpoint is exactly to NOT require that permission for UI rendering hints.
# When adding a field here, confirm it doesn't carry anything sensitive.
_UI_PREFERENCE_FIELDS: tuple[str, ...] = (
    "require_plate_clear",
    "check_printer_firmware",
    "camera_view_mode",
    "time_format",
    "date_format",
    "drying_presets",
    "ams_humidity_thresholds",
    "ams_humidity_good",
    "ams_humidity_fair",
    "ams_temp_good",
    "ams_temp_fair",
    "bed_cooled_threshold",
    # Temperature / fan-speed presets for the printer-card popovers. Numbers
    # only; no PII / credentials.
    "nozzle_temp_presets",
    "bed_temp_presets",
    "chamber_temp_presets",
    "fan_speed_presets",
)


@router.get("/ui-preferences")
async def get_ui_preferences(db: AsyncSession = Depends(get_db)):
    """Get the curated subset of settings that any page needs to render correctly.

    Intentionally not gated on SETTINGS_READ — every authenticated user (and
    every page that loads for them) needs these fields, but granting SETTINGS_READ
    would also grant visibility of secrets (SMTP/LDAP/MQTT credentials, etc.).
    Same pattern as /default-sidebar-order (#1293).

    Reuses _build_settings_response so the typed values match what /settings
    returns for fields with the same name — bool/int/float/str types stay in
    sync without a separate type-coercion path.
    """
    full = await _build_settings_response(db, is_api_key=False)
    dumped = full.model_dump()
    return {key: dumped[key] for key in _UI_PREFERENCE_FIELDS if key in dumped}


@router.get("/check-ffmpeg")
async def check_ffmpeg(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Check if ffmpeg is installed and available.

    Gated on ``SETTINGS_READ`` (audit finding I4 — the binary path was
    leaking the host filesystem layout to unauthenticated callers).
    ``require_permission_if_auth_enabled`` returns ``None`` only when
    auth is disabled (in which case there's no privacy boundary to
    enforce); otherwise it raises 401/403 before we get here.
    """
    from backend.app.services.camera import get_ffmpeg_path

    ffmpeg_path = get_ffmpeg_path()
    return {
        "installed": ffmpeg_path is not None,
        "path": ffmpeg_path,
    }


@router.get("/spoolman")
async def get_spoolman_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get Spoolman integration settings."""
    spoolman_enabled = await get_setting(db, "spoolman_enabled") or "false"
    spoolman_url = await get_setting(db, "spoolman_url") or ""
    spoolman_sync_mode = await get_setting(db, "spoolman_sync_mode") or "auto"
    spoolman_disable_weight_sync = await get_setting(db, "spoolman_disable_weight_sync") or "false"
    spoolman_report_partial_usage = await get_setting(db, "spoolman_report_partial_usage") or "true"
    auto_add_unknown_rfid = await get_setting(db, "auto_add_unknown_rfid") or "true"

    return {
        "spoolman_enabled": spoolman_enabled,
        "spoolman_url": spoolman_url,
        "spoolman_sync_mode": spoolman_sync_mode,
        "spoolman_disable_weight_sync": spoolman_disable_weight_sync,
        "spoolman_report_partial_usage": spoolman_report_partial_usage,
        "auto_add_unknown_rfid": auto_add_unknown_rfid,
    }


@router.put("/spoolman")
async def update_spoolman_settings(
    settings: dict,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Update Spoolman integration settings."""
    if "spoolman_enabled" in settings:
        old_val = await get_setting(db, "spoolman_enabled") or "false"
        new_val = settings["spoolman_enabled"]
        await set_setting(db, "spoolman_enabled", new_val)

        # Switching to Spoolman: clear built-in inventory slot assignments.
        #
        # No slot-state cleanup is needed alongside this. The AMS slot state machine is
        # DERIVED, never stored (plan §"Schema — NO NEW TABLES"): it is recomputed from
        # the live wire push plus the assignment row plus the spool's flags on every
        # pass, so deleting the rows IS the state reset. And ``slot_pipeline`` no-ops
        # outright while ``spoolman_enabled`` is true, so nothing re-creates them.
        if old_val.lower() != "true" and new_val.lower() == "true":
            from backend.app.models.spool_assignment import SpoolAssignment

            result = await db.execute(delete(SpoolAssignment))
            logger.info("Cleared %d spool assignments on switch to Spoolman mode", result.rowcount)
        # Switching back to internal mode: clear Spoolman slot assignments — the
        # symmetric counterpart of the clear above. Without this, stale
        # spoolman_slot_assignments rows linger and would wrongly count as
        # "assigned" in any mode-agnostic check (e.g. the missing-spool-
        # assignment notification, which unions both tables — #1473).
        elif old_val.lower() == "true" and new_val.lower() != "true":
            from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

            result = await db.execute(delete(SpoolmanSlotAssignment))
            logger.info("Cleared %d Spoolman slot assignments on switch to internal mode", result.rowcount)
    if "spoolman_url" in settings:
        await set_setting(db, "spoolman_url", settings["spoolman_url"])
    if "spoolman_sync_mode" in settings:
        await set_setting(db, "spoolman_sync_mode", settings["spoolman_sync_mode"])
    if "spoolman_disable_weight_sync" in settings:
        await set_setting(db, "spoolman_disable_weight_sync", settings["spoolman_disable_weight_sync"])
    if "spoolman_report_partial_usage" in settings:
        await set_setting(db, "spoolman_report_partial_usage", settings["spoolman_report_partial_usage"])
    if "auto_add_unknown_rfid" in settings:
        await set_setting(db, "auto_add_unknown_rfid", settings["auto_add_unknown_rfid"])

    spoolman_changed = "spoolman_enabled" in settings or "spoolman_url" in settings

    await db.commit()
    db.expire_all()

    if spoolman_changed:
        from backend.app.services.location_service import maybe_sync_spoolman_locations

        if await maybe_sync_spoolman_locations(db):
            await db.commit()

    # Return updated settings
    return await get_spoolman_settings(db)


async def get_homeassistant_settings(db: AsyncSession) -> dict:
    """
    Get Home Assistant integration settings.
    Environment variables (HA_URL, HA_TOKEN) take precedence over database settings.
    """
    import os

    # Check environment variables first
    ha_url_env = os.environ.get("HA_URL")
    ha_token_env = os.environ.get("HA_TOKEN")

    # Fall back to database values
    ha_url = ha_url_env or await get_setting(db, "ha_url") or ""
    ha_token = ha_token_env or await get_setting(db, "ha_token") or ""
    ha_enabled_db = await get_setting(db, "ha_enabled") or "false"

    # Track which settings come from environment
    ha_url_from_env = bool(ha_url_env)
    ha_token_from_env = bool(ha_token_env)
    ha_env_managed = ha_url_from_env and ha_token_from_env

    # Auto-enable when both env vars are set, otherwise use database value
    if ha_url_env and ha_token_env:
        ha_enabled = True
    else:
        ha_enabled = ha_enabled_db.lower() == "true"

    return {
        "ha_enabled": ha_enabled,
        "ha_url": ha_url,
        "ha_token": ha_token,
        "ha_url_from_env": ha_url_from_env,
        "ha_token_from_env": ha_token_from_env,
        "ha_env_managed": ha_env_managed,
    }


async def create_backup_zip(output_path: Path | None = None) -> tuple[Path, str]:
    """Create a complete backup ZIP (database + all data directories).

    If output_path is given, the ZIP is written there.
    Otherwise a temporary file is created (caller must clean up).
    Returns (zip_path, filename).

    Per-directory copy failures do not abort the backup, but every one of them
    is recorded in the ZIP's ``backup_manifest.json`` — read it back with
    ``_read_manifest_from_zip`` to learn whether the artifact is complete.
    """
    import json
    import shutil
    import tempfile

    from backend.app.core.db_dialect import is_sqlite

    base_dir = app_settings.base_dir
    filename = f"bambuddy-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        if is_sqlite():
            from sqlalchemy import text

            from backend.app.core.database import engine

            db_path = Path(app_settings.database_url.replace("sqlite+aiosqlite:///", ""))

            # Checkpoint WAL to ensure all data is in main db file
            async with engine.begin() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

            # Copy database file
            shutil.copy2(db_path, temp_path / "bambuddy.db")
        else:
            # PostgreSQL: export to a portable SQLite file via SQLAlchemy.
            # This makes backups restorable on both SQLite and Postgres installs.
            import sqlite3

            from backend.app.core.database import Base, engine

            backup_db_path = temp_path / "bambuddy.db"
            dst = sqlite3.connect(str(backup_db_path))
            metadata = Base.metadata

            # Create tables in SQLite backup (simplified — just column names and types)
            for table in metadata.sorted_tables:
                cols = []
                pk_cols = [col.name for col in table.columns if col.primary_key]
                for col in table.columns:
                    col_type = _sqlalchemy_type_to_sqlite_type(str(col.type))
                    # Only inline PRIMARY KEY for single-column PKs
                    pk = " PRIMARY KEY" if col.primary_key and len(pk_cols) == 1 else ""
                    cols.append(f"{col.name} {col_type}{pk}")
                # Add composite primary key constraint if needed
                if len(pk_cols) > 1:
                    cols.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
                dst.execute(f"CREATE TABLE IF NOT EXISTS {table.name} ({', '.join(cols)})")  # noqa: S608

            # Export data from Postgres to SQLite
            async with engine.connect() as conn:
                for table in metadata.sorted_tables:
                    result = await conn.execute(table.select())
                    rows = result.fetchall()
                    if not rows:
                        continue
                    columns = list(result.keys())
                    placeholders = ", ".join(["?"] * len(columns))
                    col_list = ", ".join(columns)
                    insert_sql = f"INSERT INTO {table.name} ({col_list}) VALUES ({placeholders})"  # noqa: S608  # nosec B608 — table/column names from ORM metadata, not user input

                    def _serialize_row(row):
                        return tuple(json.dumps(v) if isinstance(v, (list, dict)) else v for v in row)

                    dst.executemany(insert_sql, [_serialize_row(row) for row in rows])

            dst.commit()
            dst.close()
            logger.info("PostgreSQL backup exported to portable SQLite format")

        # Copy data directories (if they exist)
        dirs_to_backup = [
            ("archive", base_dir / "archive"),
            ("virtual_printer", base_dir / "virtual_printer"),
            ("plate_calibration", app_settings.plate_calibration_dir),
            ("icons", base_dir / "icons"),
            ("projects", base_dir / "projects"),
        ]

        copy_failures: list[dict[str, str]] = []
        included_dirs: list[str] = []

        for name, src_dir in dirs_to_backup:
            if src_dir.exists() and any(src_dir.iterdir()):
                try:
                    shutil.copytree(
                        src_dir, temp_path / name
                    )  # SEC-PATH-OK: name iterates the dirs_to_backup tuple of constant strings ("archive", "virtual_printer", ...)
                except OSError as e:
                    # shutil.Error (per-file failures) and PermissionError are
                    # both OSError; a live farm holding file locks makes them
                    # routine on Windows. The copy is best-effort, the manifest
                    # is what makes the result honest.
                    copy_failures.extend(_describe_copy_failure(name, e))
                    logger.warning("Some files in %s could not be copied: %s", name, e)
                if (
                    temp_path / name
                ).exists():  # SEC-PATH-OK: name iterates the dirs_to_backup tuple of constant strings ("archive", "virtual_printer", ...)
                    included_dirs.append(name)

        # Include the MFA encryption key as a ZIP top-level entry alongside
        # bambuddy.db. Without it, encrypted client_secret / TOTP secret rows
        # would be unrecoverable after restore on a host without MFA_ENCRYPTION_KEY set.
        from backend.app.core.paths import resolve_data_dir

        mfa_key_src = resolve_data_dir() / ".mfa_encryption_key"
        if mfa_key_src.exists() and mfa_key_src.is_file():
            try:
                shutil.copy2(mfa_key_src, temp_path / ".mfa_encryption_key")
            except OSError as exc:
                logger.error(
                    "Could not include MFA encryption key in backup (%s). "
                    "The backup ZIP will not contain the key — restore on a "
                    "keyless host will fail for encrypted secrets.",
                    exc,
                )
                raise

        manifest = {
            "manifest_version": BACKUP_MANIFEST_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "app_version": APP_VERSION,
            "build": BUILD_VERSION,
            "included_dirs": included_dirs,
            "partial": bool(copy_failures),
            "failures": copy_failures,
        }
        (
            temp_path / BACKUP_MANIFEST_NAME
        ).write_text(  # SEC-PATH-OK: BACKUP_MANIFEST_NAME is a module-level constant string
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        if copy_failures:
            logger.warning(
                "Backup is INCOMPLETE: %d file(s) could not be copied from %s",
                len(copy_failures),
                ", ".join(_manifest_partial_dirs(manifest)),
            )

        # Create ZIP
        if output_path is not None:
            zip_file = (
                output_path / filename
            )  # SEC-PATH-OK: filename = f"bambuddy-backup-{datetime.now()...}.zip" generated in create_backup_zip itself
        else:
            fd, tmp = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            zip_file = Path(tmp)

        with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in temp_path.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(temp_path)
                    zf.write(file_path, arcname)

    return zip_file, filename


@router.get("/backup")
async def create_backup(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_BACKUP),
):
    """Create a complete backup (database + all files) as a ZIP download.

    An incomplete backup is still served, but never silently: the response
    carries ``X-Backup-Partial: true`` plus an ``X-Backup-Warning`` naming the
    affected directories, and the ZIP's manifest lists every failed path.
    """
    from starlette.background import BackgroundTask

    try:
        zip_file, filename = await create_backup_zip()
        headers: dict[str, str] = {}
        manifest = _read_manifest_from_zip(zip_file)
        if manifest and manifest.get("partial"):
            failures = manifest.get("failures") or []
            warning = (
                f"This backup is INCOMPLETE: {len(failures)} file(s) could not be copied from "
                f"{', '.join(_manifest_partial_dirs(manifest)) or 'unknown directories'}. "
                "Restoring it will replace current data with a partial copy."
            )
            # HTTP header values are latin-1; manifest text is not guaranteed ASCII.
            headers["X-Backup-Partial"] = "true"
            headers["X-Backup-Warning"] = warning.encode("ascii", "replace").decode("ascii")
            logger.warning("Serving a partial backup ZIP: %s", warning)
        return FileResponse(
            path=zip_file,
            filename=filename,
            media_type="application/zip",
            headers=headers or None,
            background=BackgroundTask(lambda: zip_file.unlink(missing_ok=True)),
        )
    except Exception as e:
        logger.error("Backup failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Backup failed. Check server logs for details."},
        )


async def _import_sqlite_to_postgres(sqlite_path: Path, postgres_url: str):
    """Import data from a SQLite database file into the current PostgreSQL database.

    Used for cross-database restore (SQLite backup → PostgreSQL).
    Reads all tables from the SQLite file and bulk-inserts into Postgres.
    """
    import sqlite3

    from sqlalchemy import text

    from backend.app.core.database import Base, _create_engine

    # Create a temporary engine for the import (current engine was disposed)
    pg_engine = _create_engine()

    try:
        # Open SQLite file directly (sync — it's a local file read)
        src = sqlite3.connect(str(sqlite_path))
        src.row_factory = sqlite3.Row

        # Get list of tables from SQLite (skip internal/FTS tables)
        cursor = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'archive_fts%'"
        )
        src_tables = {row["name"] for row in cursor.fetchall()}

        # Get Postgres tables from our ORM models
        metadata = Base.metadata
        pg_tables = set(metadata.tables.keys())

        # Only import tables that exist in both source and destination
        tables_to_import = src_tables & pg_tables
        sorted_tables = [t.name for t in metadata.sorted_tables if t.name in tables_to_import]

        # Phase 1: Drop all tables and recreate WITHOUT foreign keys.
        # This avoids all FK ordering/orphan issues during import.
        saved_fks = {}
        for table in metadata.sorted_tables:
            fks = list(table.foreign_key_constraints)
            if fks:
                saved_fks[table.name] = fks
                for fk in fks:
                    table.constraints.discard(fk)

        async with pg_engine.begin() as conn:
            # Cap how long DROP TABLE will wait for AccessExclusiveLock so
            # any residual concurrent writer (per-printer MQTT clients
            # writing reactively, an AMS history recorder firing on its
            # hourly cadence) surfaces a fast `lock_timeout` error instead
            # of blocking the restore for 30 s or producing a deadlock.
            # SET LOCAL scopes to this transaction only; outside this
            # restore path the global default (no timeout) applies.
            await conn.execute(text("SET LOCAL lock_timeout = '10s'"))

            # Drop every existing table in the public schema with CASCADE
            # rather than `metadata.drop_all`. Two reasons:
            #   1. The user's live DB may carry orphan tables from removed
            #      features (e.g. the legacy `spoolman_slot_assignments`,
            #      `spoolman_k_profile`) that hold FK constraints back to
            #      ORM tables. `drop_all` doesn't know they exist and emits
            #      `DROP TABLE printers` without CASCADE — Postgres refuses
            #      and the whole restore aborts (#XXXX).
            #   2. Even within the metadata, `drop_all` is FK-ordered and
            #      breaks if a future schema rename leaves old constraints
            #      around. CASCADE is the right tool for a destructive
            #      restore: the user is intentionally wiping state.
            await conn.execute(
                text(
                    "DO $$ DECLARE r RECORD; BEGIN "
                    "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP "
                    "EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE'; "
                    "END LOOP; END $$;"
                )
            )
            await conn.run_sync(metadata.create_all)

        # Restore FK definitions in metadata (needed for re-adding later)
        for table_name, fks in saved_fks.items():
            table_obj = metadata.tables[table_name]
            for fk in fks:
                table_obj.constraints.add(fk)

        # Phase 2: Import data (no FKs to worry about)
        async with pg_engine.begin() as conn:
            # Import each table in dependency order (parents before children)
            for table_name in sorted_tables:
                rows = src.execute(f"SELECT * FROM {table_name}").fetchall()  # noqa: S608  # nosec B608
                if not rows:
                    continue

                # Filter to columns that exist in the Postgres table
                src_columns = rows[0].keys()
                pg_table = metadata.tables.get(table_name)
                pg_columns = {c.name for c in pg_table.columns} if pg_table is not None else set()
                columns = [c for c in src_columns if c in pg_columns]

                if not columns:
                    continue

                col_list = ", ".join(columns)
                param_list = ", ".join(f":{c}" for c in columns)
                # ON CONFLICT DO NOTHING handles duplicate rows from SQLite (which doesn't enforce unique constraints)
                insert_sql = text(f"INSERT INTO {table_name} ({col_list}) VALUES ({param_list}) ON CONFLICT DO NOTHING")  # noqa: S608  # nosec B608

                # Identify columns that need type conversion (SQLite stores booleans
                # as int and datetimes as str — asyncpg requires native Python types)
                from datetime import datetime as dt

                bool_columns = set()
                datetime_columns = set()
                not_null_defaults = {}  # col_name -> default value for NOT NULL columns
                if pg_table is not None:
                    for col in pg_table.columns:
                        if col.name not in columns:
                            continue
                        col_type = str(col.type)
                        if col_type == "BOOLEAN":
                            bool_columns.add(col.name)
                        elif col_type in ("DATETIME", "TIMESTAMP WITHOUT TIME ZONE", "TIMESTAMP WITH TIME ZONE"):
                            datetime_columns.add(col.name)
                        # Track NOT NULL columns with defaults — older backups may have NULL
                        # for columns added after the backup was created
                        if not col.nullable:
                            if col.default is not None:
                                default = col.default.arg
                                if callable(default):
                                    default = default(None)
                                not_null_defaults[col.name] = default
                            elif col.server_default is not None:
                                # server_default=func.now() → use current timestamp
                                if col.name in datetime_columns:
                                    not_null_defaults[col.name] = "__now__"
                                else:
                                    # Try to extract literal server default
                                    sd = str(col.server_default.arg) if hasattr(col.server_default, "arg") else None
                                    if sd is not None:
                                        not_null_defaults[col.name] = sd

                now = dt.now()

                def _convert_row(
                    row, cols=columns, bools=bool_columns, dts=datetime_columns, nn_defaults=not_null_defaults, _now=now
                ):
                    result = {}
                    for c in cols:
                        val = row[c]
                        if val is None and c in nn_defaults:
                            val = _now if nn_defaults[c] == "__now__" else nn_defaults[c]
                        if val is not None:
                            if c in bools:
                                val = bool(val)
                            elif c in dts and isinstance(val, str):
                                try:
                                    val = dt.fromisoformat(val)
                                except ValueError:
                                    pass
                        result[c] = val
                    return result

                batch = [_convert_row(row) for row in rows]
                await conn.execute(insert_sql, batch)
                logger.info("Imported %d rows into %s", len(batch), table_name)

            # Reset sequences to max(id) + 1 for each table with an id column
            for table_name in sorted_tables:
                try:
                    async with conn.begin_nested():
                        result = await conn.execute(text(f"SELECT MAX(id) FROM {table_name}"))  # noqa: S608  # nosec B608
                        max_id = result.scalar()
                        if max_id is not None:
                            seq_name = f"{table_name}_id_seq"
                            await conn.execute(text(f"SELECT setval('{seq_name}', {max_id})"))  # noqa: S608
                except Exception:
                    pass  # Table may not have an id column or sequence

        src.close()
        logger.info("Cross-database import complete: %d tables imported", len(tables_to_import))

        # Recreate FK constraints from ORM metadata (not from saved definitions).
        # Use individual transactions so orphaned SQLite data doesn't block valid FKs.
        from sqlalchemy.schema import AddConstraint

        failed_fks = []
        for table in metadata.sorted_tables:
            for fk in table.foreign_key_constraints:
                try:
                    async with pg_engine.begin() as fk_conn:
                        await fk_conn.execute(AddConstraint(fk))
                except Exception:
                    failed_fks.append(f"{table.name}.{fk.name}")
        if failed_fks:
            logger.warning(
                "Could not restore %d FK constraints (orphaned data in SQLite): %s",
                len(failed_fks),
                ", ".join(failed_fks),
            )

    finally:
        await pg_engine.dispose()


@router.post("/restore")
async def restore_backup(
    file: UploadFile = File(...),
    force_partial: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_RESTORE),
):
    """Restore from a complete backup ZIP.

    Replaces the database and all data directories from the backup ZIP.
    Requires a restart after restore.

    A backup whose manifest records it as incomplete is refused with 409 unless
    the ``force_partial=true`` query parameter is passed. Data directories are
    replaced transactionally: the current contents are moved aside first and
    moved back if any step fails, so a failed restore never destroys the
    library. A ZIP with no manifest (written by an older build) is accepted and
    the absence is reported in the response.
    """
    import shutil
    import tempfile

    from fastapi import HTTPException

    from backend.app.core.database import close_all_connections, init_db, reinitialize_database
    from backend.app.core.db_dialect import is_sqlite
    from backend.app.services.virtual_printer import virtual_printer_manager

    base_dir = app_settings.base_dir

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)

        # 1. Read and extract ZIP
        content = await file.read()

        # Check if it's a valid ZIP
        if not file.filename or not file.filename.endswith(".zip"):
            raise HTTPException(400, "Invalid backup file: must be a .zip file")

        try:
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                for name in zf.namelist():
                    # Reject path-traversal payloads: any entry whose resolved
                    # path escapes temp_path would allow writing arbitrary files
                    # on the host (ZipSlip / CVE-2006-5456).
                    dest = (
                        temp_path / name
                    ).resolve()  # SEC-PATH-OK: is_relative_to containment check below before extractall
                    # is_relative_to (Python 3.9+) covers both relative
                    # path-traversal (../etc/passwd) and absolute-path overrides
                    # (/etc/passwd) — str.startswith was vulnerable to
                    # prefix-collision attacks (e.g. /tmp/abc_evil/file passing
                    # a /tmp/abc prefix check).
                    if not dest.is_relative_to(temp_path.resolve()):
                        raise HTTPException(400, f"Invalid backup: unsafe path in ZIP: {name!r}")
                zf.extractall(temp_path)
        except zipfile.BadZipFile:
            raise HTTPException(400, "Invalid backup file: not a valid ZIP")

        # 2. Validate backup
        backup_db = temp_path / "bambuddy.db"
        if not backup_db.exists():
            raise HTTPException(400, "Invalid backup: missing bambuddy.db")

        # 2b. Completeness gate — refuse a known-partial backup before anything
        # destructive runs. Nothing below this point is reversible for free.
        manifest_path = (
            temp_path / BACKUP_MANIFEST_NAME
        )  # SEC-PATH-OK: BACKUP_MANIFEST_NAME is a module-level constant string
        manifest_present = manifest_path.is_file()
        manifest = _parse_manifest(manifest_path.read_bytes()) if manifest_present else None
        refusal = _partial_refusal_reason(manifest_present, manifest)
        if refusal and not force_partial:
            # This endpoint's own contract is {success, message} (the only
            # client reads those keys), so a refusal must not hide behind
            # HTTPException's `detail`.
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "message": (
                        f"{refusal} Nothing was changed. Re-send with force_partial=true to restore it anyway."
                    ),
                    "manifest_present": manifest_present,
                },
            )

        # (name, destination, pre-restore copy) for every directory whose old
        # contents are still recoverable — drained at the commit point.
        moved_aside: list[tuple[str, Path, Path]] = []

        try:
            import asyncio

            # 3. Stop virtual printer if running (releases file locks)
            try:
                if virtual_printer_manager.is_enabled:
                    logger.info("Stopping virtual printer for restore...")
                    await virtual_printer_manager.configure(enabled=False)
                    await asyncio.sleep(1)
            except Exception as e:
                logger.warning("Failed to stop virtual printer: %s", e)

            # 3b. Pause timer-based background services BEFORE the DB swap.
            # close_all_connections() below only disposes the engine's pool,
            # not the asyncio tasks that opened sessions from it. The print
            # scheduler (30 s cadence), smart-plug snapshot loop (30 s), and
            # notification digest loop all
            # wake up and call async_session(), which lazily re-creates a
            # pool connection holding RowExclusiveLock on print_queue /
            # smart_plug_energy_snapshots / etc. The DROP TABLE CASCADE
            # pass in the PostgreSQL restore path needs AccessExclusiveLock
            # on every public table, producing an AB/BA deadlock and a
            # full restore rollback. Successful restore already requires a
            # container restart, so we don't restart the services here.
            try:
                from backend.app.services.notification_service import notification_service
                from backend.app.services.print_scheduler import scheduler as print_scheduler
                from backend.app.services.smart_plug_manager import smart_plug_manager

                logger.info("Pausing background services for restore...")
                print_scheduler.stop()
                smart_plug_manager.stop_scheduler()
                notification_service.stop_digest_scheduler()
                # In-flight loop iterations need a moment to commit + release
                # their DB sessions before we dispose() the engine pool.
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.warning("Could not cleanly pause background services: %s", e)

            # 4. Close current database connections
            logger.info("Closing database connections...")
            await close_all_connections()

            # 5. Replace data directories BEFORE any irreversible step.
            # base_dir/archive holds every uploaded library file and every
            # archive copy, so the old wipe-then-copy destroyed the library
            # whenever a copy hit a locked file. Each destination's contents are
            # MOVED to a sibling "<name>.pre-restore-<UTC>" — a rename on the
            # same volume, no disk cost — and moved back if any step through the
            # database swap fails. The destination directory itself is never
            # renamed: on Docker it can be a bind-mount point.
            dirs_to_restore = [
                ("archive", base_dir / "archive"),
                ("virtual_printer", base_dir / "virtual_printer"),
                ("plate_calibration", app_settings.plate_calibration_dir),
                ("icons", base_dir / "icons"),
                ("projects", base_dir / "projects"),
            ]
            aside_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

            for name, dest_dir in dirs_to_restore:
                src_dir = (
                    temp_path / name
                )  # SEC-PATH-OK: name iterates the dirs_to_restore tuple of constant strings ("archive", "virtual_printer", ...)
                if not src_dir.exists():
                    continue
                logger.info("Restoring %s directory...", name)
                aside_dir = dest_dir.parent / f"{dest_dir.name}.pre-restore-{aside_stamp}"
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    _move_dir_contents(dest_dir, aside_dir)
                    moved_aside.append((name, dest_dir, aside_dir))
                    for item in src_dir.iterdir():
                        dest_item = dest_dir / item.name
                        if item.is_dir():
                            shutil.copytree(item, dest_item)
                        else:
                            shutil.copy2(item, dest_item)
                except OSError as e:
                    logger.error("Restore of the %s directory failed: %s", name, e, exc_info=True)
                    note = _rollback_restored_dirs(moved_aside)
                    return JSONResponse(
                        status_code=500,
                        content={
                            "success": False,
                            "message": (
                                f"Restore aborted while restoring the '{name}' directory: {e}. "
                                f"The database was not modified. {note}"
                            ).strip(),
                        },
                    )

            # B1: Restore the MFA encryption key file BEFORE the database swap.
            # If the key write fails (OSError, RO disk, full disk, EACCES) we
            # can still abort while the live DB is intact. Doing this AFTER the
            # DB swap would leave the database with rows encrypted under the
            # backup's key but the running install holding only the old key —
            # every encrypted secret becomes unrecoverable.
            from backend.app.core.paths import resolve_data_dir

            mfa_key_src = temp_path / ".mfa_encryption_key"
            if mfa_key_src.exists() and mfa_key_src.is_file():
                dst_key = resolve_data_dir() / ".mfa_encryption_key"
                tmp_key = dst_key.parent / ".mfa_encryption_key.restore-tmp"
                try:
                    dst_key.parent.mkdir(parents=True, exist_ok=True)
                    # S1: atomic write with restrictive mode from creation.
                    # O_TRUNC because a stale tmp may exist from a prior
                    # failed restore attempt — we want to overwrite it.
                    fd = os.open(str(tmp_key), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        os.write(fd, mfa_key_src.read_bytes())
                    finally:
                        os.close(fd)
                    # POSIX rename(2) — atomic when source/dest are on the
                    # same filesystem (we're staying inside dst_key.parent).
                    os.replace(str(tmp_key), str(dst_key))
                    # S9: warn if the FS doesn't enforce 0o600
                    actual_mode = dst_key.stat().st_mode & 0o777
                    if actual_mode != 0o600:
                        logger.warning(
                            "Restored MFA key file %s: filesystem did not enforce 0o600 "
                            "(actual: 0o%o). Key may be world-readable on Windows / SMB / FUSE.",
                            dst_key,
                            actual_mode,
                        )
                    logger.info("Restored .mfa_encryption_key from backup")
                except OSError as e:
                    logger.error(
                        "Could not write restored MFA key file to %s: %s — "
                        "aborting BEFORE database swap (DB unchanged).",
                        dst_key,
                        e,
                        exc_info=True,
                    )
                    note = _rollback_restored_dirs(moved_aside)
                    raise HTTPException(
                        status_code=500,
                        detail=" ".join(
                            part
                            for part in (
                                "Restore aborted: MFA key write failed. Database is unchanged.",
                                note,
                                "Check server logs.",
                            )
                            if part
                        ),
                    ) from e

            # 6. Replace database
            logger.info("Restoring database from backup...")
            if is_sqlite():
                db_path = Path(app_settings.database_url.replace("sqlite+aiosqlite:///", ""))
                # Use SQLite's online backup API instead of shutil.copy2.
                # The pragma at database.py:19 runs the live DB in WAL mode,
                # which means a naive file copy is unsafe: anything written
                # to the live DB before this call that hasn't been
                # checkpointed yet (seed_default_groups + init_db on first
                # start, plus whatever background heartbeats wrote during
                # the request window) sits in bambuddy.db-wal with valid
                # checksums. The route handler's own `db: Depends(get_db)`
                # session also keeps a connection checked out across
                # engine.dispose(), holding fds to the WAL inode. With
                # `shutil.copy2` SQLite finds the stale WAL on the next
                # open and silently re-applies those page-level writes on
                # top of the restored DB, partially clobbering it with
                # fresh-install state — the user sees a "successful"
                # restore where most rows and settings have reverted to
                # defaults (#1211 / #668). The page-by-page backup API
                # opens both DBs as real SQLite connections, takes the
                # right locks, and routes new pages through the live DB's
                # own WAL — so concurrent open sessions see their own
                # snapshot until they close (transaction isolation) but
                # can't corrupt the restored state.
                import sqlite3

                src_conn = sqlite3.connect(str(backup_db))
                try:
                    dst_conn = sqlite3.connect(str(db_path))
                    try:
                        src_conn.backup(dst_conn)
                    finally:
                        dst_conn.close()
                finally:
                    src_conn.close()
            else:
                # Import SQLite backup into PostgreSQL
                logger.info("Importing SQLite backup into PostgreSQL...")
                await _import_sqlite_to_postgres(backup_db, app_settings.database_url)

            # 7. Commit point: every directory and the database are in place, so
            # the pre-restore copies are no longer a rollback target.
            cleanup_note = _discard_aside_dirs(moved_aside)

            # 8. Reset the encryption singleton so the migration that runs
            # inside init_db() picks up the restored key file (if a new one
            # was written above). Without this reset, _get_fernet would
            # return the cached Fernet instance built from the previous key.
            import backend.app.core.encryption as _enc_mod

            _enc_mod._fernet_instance = None
            _enc_mod._key_source = None
            _enc_mod._warn_shown = False

            # 9. Reinitialize the database engine and apply schema migrations so that
            # tables added after the backup was created (e.g. ams_labels) exist
            # immediately, without requiring a manual restart.
            await reinitialize_database()
            await init_db()

            logger.info("Restore complete - restart required")
            parts = ["Backup restored successfully. Please restart Bambuddy for changes to take effect."]
            if not manifest_present:
                parts.append(
                    "Note: this backup has no manifest (written by an older version), "
                    "so its completeness could not be verified."
                )
            if refusal:
                parts.append(f"WARNING: restored a backup recorded as incomplete (force_partial). {refusal}")
            if cleanup_note:
                parts.append(cleanup_note)
            return {
                "success": True,
                "message": " ".join(parts),
                "manifest_present": manifest_present,
                "restored_partial": bool(refusal),
            }

        except HTTPException:
            # Preserve specific HTTP error responses raised inside the restore
            # body (e.g. the key-write OSError → 500). The blanket
            # except Exception below would otherwise swallow them and replace
            # the operator-facing detail with a generic message.
            # Each raise site rolls back first, so this drained call is a no-op.
            _rollback_restored_dirs(moved_aside)
            raise
        except Exception as e:
            logger.error("Restore failed: %s", e, exc_info=True)
            note = _rollback_restored_dirs(moved_aside)
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "message": " ".join(
                        part for part in ("Restore failed. Check server logs for details.", note) if part
                    ),
                },
            )


@router.get("/network-interfaces")
async def get_network_interfaces(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get available network interfaces with all IPs (primary + aliases)."""
    from backend.app.services.network_utils import get_all_interface_ips

    interfaces = get_all_interface_ips()
    return {"interfaces": interfaces}


@router.get("/virtual-printer/models")
async def get_virtual_printer_models(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get available virtual printer models."""
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        VIRTUAL_PRINTER_MODELS,
    )

    return {
        "models": VIRTUAL_PRINTER_MODELS,
        "default": DEFAULT_VIRTUAL_PRINTER_MODEL,
    }


@router.get("/virtual-printer")
async def get_virtual_printer_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get virtual printer settings and status."""
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        virtual_printer_manager,
    )

    enabled = await get_setting(db, "virtual_printer_enabled")
    access_code = await get_setting(db, "virtual_printer_access_code")
    mode = await get_setting(db, "virtual_printer_mode")
    model = await get_setting(db, "virtual_printer_model")
    target_printer_id = await get_setting(db, "virtual_printer_target_printer_id")
    remote_interface_ip = await get_setting(db, "virtual_printer_remote_interface_ip")
    tailscale_disabled_raw = await get_setting(db, "virtual_printer_tailscale_disabled")
    archive_name_source = await get_setting(db, "virtual_printer_archive_name_source")

    from backend.app.models.virtual_printer import VP_MODE_ARCHIVE, normalize_vp_mode

    return {
        "enabled": enabled == "true" if enabled else False,
        "access_code_set": bool(access_code),
        # Normalize on read so older settings rows (with `immediate` /
        # `print_queue`) come out as `archive` / `queue` for the frontend.
        "mode": normalize_vp_mode(mode) or VP_MODE_ARCHIVE,
        "model": model or DEFAULT_VIRTUAL_PRINTER_MODEL,
        "target_printer_id": int(target_printer_id) if target_printer_id else None,
        "remote_interface_ip": remote_interface_ip or "",
        "tailscale_disabled": tailscale_disabled_raw == "true" if tailscale_disabled_raw else True,
        "archive_name_source": archive_name_source if archive_name_source in ("metadata", "filename") else "metadata",
        "status": virtual_printer_manager.get_status(),
    }


@router.put("/virtual-printer")
async def update_virtual_printer_settings(
    enabled: bool = None,
    access_code: str = None,
    mode: str = None,
    model: str = None,
    target_printer_id: int = None,
    remote_interface_ip: str = None,
    tailscale_disabled: bool = None,
    archive_name_source: str = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_UPDATE),
):
    """Update virtual printer settings and restart services if needed.

    For proxy mode with SSDP proxy (dual-homed setup):
    - remote_interface_ip: IP of interface on slicer's network (LAN B)
    - Local interface is auto-detected based on target printer IP
    """
    from sqlalchemy import select

    from backend.app.models.printer import Printer
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        VIRTUAL_PRINTER_MODELS,
        virtual_printer_manager,
    )

    # Get current values
    current_enabled = await get_setting(db, "virtual_printer_enabled") == "true"
    current_access_code = await get_setting(db, "virtual_printer_access_code") or ""
    # Default to `archive` (the canonical name) but tolerate legacy `immediate`
    # in the stored value — normalized later before validation.
    current_mode = await get_setting(db, "virtual_printer_mode") or "archive"
    current_model = await get_setting(db, "virtual_printer_model") or DEFAULT_VIRTUAL_PRINTER_MODEL
    current_target_id_str = await get_setting(db, "virtual_printer_target_printer_id")
    current_target_id = int(current_target_id_str) if current_target_id_str else None
    current_remote_iface = await get_setting(db, "virtual_printer_remote_interface_ip") or ""
    current_ts_disabled_raw = await get_setting(db, "virtual_printer_tailscale_disabled")
    # Default True (opt-in) when the setting has never been saved — matches the model default.
    current_ts_disabled = current_ts_disabled_raw == "true" if current_ts_disabled_raw else True

    # Apply updates
    new_enabled = enabled if enabled is not None else current_enabled
    new_access_code = access_code if access_code is not None else current_access_code
    new_mode = mode if mode is not None else current_mode
    new_model = model if model is not None else current_model
    new_target_id = target_printer_id if target_printer_id is not None else current_target_id
    new_remote_iface = remote_interface_ip if remote_interface_ip is not None else current_remote_iface
    new_ts_disabled = tailscale_disabled if tailscale_disabled is not None else current_ts_disabled

    # Validate mode. Canonical wire values are `archive` / `review` / `queue`
    # / `proxy`; legacy `immediate` and `print_queue` are accepted as aliases
    # and translated before storage so support bundles stop showing the old
    # confusing pair (#1429 mode-label discrepancy).
    from backend.app.models.virtual_printer import VP_MODE_VALUES, normalize_vp_mode

    canonical_mode = normalize_vp_mode(new_mode)
    if canonical_mode not in VP_MODE_VALUES:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Mode must be one of: {', '.join(VP_MODE_VALUES)}",
            },
        )
    new_mode = canonical_mode

    # Validate archive_name_source
    if archive_name_source is not None and archive_name_source not in ("metadata", "filename"):
        return JSONResponse(
            status_code=400,
            content={"detail": "archive_name_source must be 'metadata' or 'filename'"},
        )

    # Validate model
    if model is not None and model not in VIRTUAL_PRINTER_MODELS:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Invalid model. Must be one of: {', '.join(VIRTUAL_PRINTER_MODELS.keys())}"},
        )

    # Mode-specific validation and printer lookup
    target_printer_ip = ""
    target_printer_serial = ""
    if new_mode == "proxy":
        # Proxy mode requires target printer when enabling
        if new_enabled and not new_target_id:
            # If just switching to proxy mode (not explicitly enabling), auto-disable
            if enabled is None:
                new_enabled = False
            else:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Target printer is required for proxy mode"},
                )

        # Look up printer IP and serial if we have a target
        if new_target_id:
            result = await db.execute(select(Printer).where(Printer.id == new_target_id))
            printer = result.scalar_one_or_none()
            if not printer:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Printer with ID {new_target_id} not found"},
                )
            target_printer_ip = printer.ip_address
            target_printer_serial = printer.serial_number
        # Access code not required for proxy mode
    else:
        # Non-proxy modes require access code when enabling
        if new_enabled and not new_access_code:
            # If just switching modes (not explicitly enabling), auto-disable
            if enabled is None:
                new_enabled = False
            else:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Access code is required when enabling virtual printer"},
                )

        # Validate access code length (Bambu Studio requires exactly 8 characters)
        if access_code is not None and access_code and len(access_code) != 8:
            return JSONResponse(
                status_code=400,
                content={"detail": "Access code must be exactly 8 characters"},
            )

    # Save settings
    await set_setting(db, "virtual_printer_enabled", "true" if new_enabled else "false")
    if access_code is not None:
        await set_setting(db, "virtual_printer_access_code", access_code)
    await set_setting(db, "virtual_printer_mode", new_mode)
    if model is not None:
        await set_setting(db, "virtual_printer_model", model)
    if target_printer_id is not None:
        await set_setting(db, "virtual_printer_target_printer_id", str(target_printer_id))
    if remote_interface_ip is not None:
        await set_setting(db, "virtual_printer_remote_interface_ip", remote_interface_ip)
    if tailscale_disabled is not None:
        await set_setting(db, "virtual_printer_tailscale_disabled", "true" if tailscale_disabled else "false")
    if archive_name_source is not None:
        await set_setting(db, "virtual_printer_archive_name_source", archive_name_source)

    # Propagate tailscale_disabled to the first VirtualPrinter row so sync_from_db() picks it up
    if tailscale_disabled is not None:
        from backend.app.models.virtual_printer import VirtualPrinter as VPModel

        vp_result = await db.execute(select(VPModel).order_by(VPModel.position).limit(1))
        first_vp = vp_result.scalar_one_or_none()
        if first_vp is not None:
            first_vp.tailscale_disabled = new_ts_disabled

    await db.commit()
    db.expire_all()

    # Reconfigure virtual printer
    try:
        await virtual_printer_manager.configure(
            enabled=new_enabled,
            access_code=new_access_code,
            mode=new_mode,
            model=new_model,
            target_printer_ip=target_printer_ip,
            target_printer_serial=target_printer_serial,
            remote_interface_ip=new_remote_iface,
        )
    except ValueError as e:
        logger.warning("Virtual printer configuration validation error: %s", e)
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid virtual printer configuration. Check the provided values."},
        )
    except Exception as e:
        logger.error("Failed to configure virtual printer: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": "Failed to configure virtual printer. Check server logs for details."},
        )

    return await get_virtual_printer_settings(db)


# =============================================================================
# MQTT Relay Settings
# =============================================================================


@router.get("/mqtt/status")
async def get_mqtt_status(
    _: User | None = RequirePermissionIfAuthEnabled(Permission.SETTINGS_READ),
):
    """Get MQTT relay connection status."""
    from backend.app.services.mqtt_relay import mqtt_relay

    return mqtt_relay.get_status()
