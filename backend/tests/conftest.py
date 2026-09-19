"""Shared test fixtures for BamBuddy backend tests.

Composition root. This module owns the process-wide setup that has to happen
before the application is imported, and wires in the fixtures that live in
``backend/tests/_fixtures/``: ``db.py`` (engines, sessions, schema), ``clock.py``
(the steerable monotonic clock) and ``ast_tree.py`` (the parsed view of
``backend/app``). Resources belong there; composition belongs here.
"""

import atexit
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# IMPORTANT: Set environment variables BEFORE any app imports
# This must happen before settings/config are loaded
os.environ["LOG_TO_FILE"] = "false"
os.environ["DEBUG"] = "false"

# ONE data root for this worker, established before `backend.app.core.config` is
# imported so every derived path (base_dir, archive_dir, database_url,
# plate_calibration_dir, erp_config_file) lands in it. Without this the harness
# inherits the developer's real data root: the module-level engine in
# `core/database.py` then points at the repo's own 15 MB bambuddy.db, and the 15
# modules that bound `async_session` at import — which no conftest patch can
# reach — quietly write to it (see the contemporaneous note at
# integration/test_security.py:1340-1349).
#
# The directory MUST be `<root>/<worker>/data`, not `<root>/<worker>`: config.py
# derives the config dir from DATA_DIR's PARENT, so a bare worker dir would give
# every xdist worker the same `<root>/config/erp.env`.
# An operator pin is DATA_DIR set from outside, which we can only recognise by
# the absence of our own root marker: under xdist the controller imports this
# module first and exports DATA_DIR, so the workers inherit a value that looks
# exactly like a pin and must NOT be reused -- sharing it gave every worker the
# same database and `table printers already exists` 6,737 times.
_TEST_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "master")
# Deliberately NOT a BAMBUDDY_*/MFA_* name: config.py:184 scans those two
# prefixes for typos and would rightly warn that this is not a declared Settings
# field. It is harness state, so it sits in pytest's namespace beside
# PYTEST_XDIST_WORKER.
_ROOT_ENV = "PYTEST_BAMBUDDY_DATA_ROOT"
_OPERATOR_PINNED_DATA_DIR = bool(os.environ.get("DATA_DIR")) and not os.environ.get(_ROOT_ENV)

if _OPERATOR_PINNED_DATA_DIR:
    _TEST_ROOT_DIR = None
else:
    # The root is per RUN (created by whoever arrives first, inherited by the
    # workers through the environment); the data dir under it is per WORKER.
    _root = os.environ.get(_ROOT_ENV)
    if not _root:
        _root = tempfile.mkdtemp(prefix="bambuddy_tests_")
        os.environ[_ROOT_ENV] = _root
    _TEST_ROOT_DIR = Path(_root)
    _TEST_DATA_DIR = _TEST_ROOT_DIR / _TEST_WORKER / "data"
    _TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Assignment, not setdefault: see above -- the inherited value is the
    # controller's, and reusing it is the bug.
    os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)


def _cleanup_test_root_dir():
    # Sweep only this process's own worker subtree. A worker that removed the
    # shared root would delete its siblings' databases mid-run; the controller
    # owns the root itself and gets it on the way out.
    if _TEST_ROOT_DIR is None:
        return
    if _TEST_WORKER == "master":
        shutil.rmtree(_TEST_ROOT_DIR, ignore_errors=True)
    else:
        shutil.rmtree(_TEST_ROOT_DIR / _TEST_WORKER, ignore_errors=True)


atexit.register(_cleanup_test_root_dir)

# No test opens a socket off this machine. Installed here, before the app is
# imported, for the same reason the data root is: it is process-wide state that
# has to be in place before anything can use it. The rule, why it is
# fail-open, and what it deliberately allows (127.0.0.1 — the real pyftpdlib
# FTPS server, the virtual-printer MQTT/FTP servers, asyncio's Windows
# self-pipe) live in the module.
from backend.tests._fixtures.netguard import install_outbound_connect_guard  # noqa: E402

install_outbound_connect_guard()

from httpx import ASGITransport, AsyncClient  # noqa: E402

# Ensure settings use our env vars - import and override before database import
from backend.app.core.config import settings  # noqa: E402
from backend.app.core.paths import resolve_data_dir  # noqa: E402

settings.log_to_file = False

# Fixtures that own a resource live in _fixtures/ and are re-exported here, so
# every test file sees them by name with nothing to import.
from backend.tests._fixtures.ast_tree import app_sources  # noqa: E402,F401
from backend.tests._fixtures.clock import clock, make_clock, retry_window_clock  # noqa: E402,F401
from backend.tests._fixtures.db import (  # noqa: E402,F401
    db_session,
    dispose_live_engine_after_module,
    force_sqlite_dialect,
    live_app_database_schema,
    module_database,
    own_session_factory,
    test_engine,
)


@pytest.fixture(autouse=True)
def assert_one_data_root() -> Generator[None, None, None]:
    """The pin that fires if the two data-root authorities ever diverge again.

    ``settings`` derives every path from ``paths.resolve_data_dir()``; if a test
    (or a future edit) reintroduces a second reader, this is where it surfaces —
    at the end of the test that did it, not three files later as a mystery write
    to the real database.
    """
    yield
    assert Path(settings.base_dir) == resolve_data_dir(), (
        "settings.base_dir and paths.resolve_data_dir() disagree: "
        f"{settings.base_dir!r} != {resolve_data_dir()!r}. "
        "The data root has one authority (core/paths.py) — see "
        "backend/tests/unit/test_fixture_ownership.py."
    )


@pytest.fixture(autouse=True)
def mfa_encryption_isolation(monkeypatch):
    """Per-test isolation for MFA encryption state.

    - Removes any inherited ``MFA_ENCRYPTION_KEY`` env var.
    - Resets the ``encryption`` module-level singletons before AND after the
      test so reorder doesn't leak cached Fernet instances.

    It no longer sets ``DATA_DIR``: the harness pins one data root for the whole
    worker at import (top of this file), which is what keeps
    ``.mfa_encryption_key`` out of the repo and out of the other workers' way.
    Every test that needs a *per-test* data dir already sets it itself —
    ``integration/test_security.py`` does exactly that at :149, :165, :186, :198,
    :222, :260, :290, :313, :334, :1438, :1454 and throughout the backup/restore
    class — so this fixture setting it too made two writers of one fact, and the
    one that lost was the one that ran first.

    Tests that want to exercise an active key should call
    ``monkeypatch.setenv("MFA_ENCRYPTION_KEY", valid_key)`` and
    ``enc_mod._fernet_instance = None`` inside the test body — the autouse
    fixture only sets defaults, it doesn't lock them in. A test asserting on
    ``key_source`` must point ``DATA_DIR`` at its own ``tmp_path`` first, or it
    will read the key a previous test generated in the worker's data dir.
    """
    from backend.app.core import encryption as enc_mod

    monkeypatch.delenv("MFA_ENCRYPTION_KEY", raising=False)
    enc_mod._fernet_instance = None
    enc_mod._warn_shown = False
    enc_mod._key_source = None
    yield
    enc_mod._fernet_instance = None
    enc_mod._warn_shown = False
    enc_mod._key_source = None


@pytest.fixture(autouse=True)
def reset_plate_occupancy_authority():
    """Start and leave every test with an empty, UN-WIRED occupancy authority.

    ``plate_occupancy`` is a process singleton: the plate gate, the pending eject
    and the dispatch lease are ONE record per printer, read by dispatch admission,
    the eject lanes and the status payloads alike. A record (or a wired
    persist/broadcast/kick/policy-driver callable) left standing by one test
    module silently decides another module's behaviour — the exact order-dependent
    leak that made ``test_capability_gate_api`` fail only inside a multi-file run.
    One shared reset here covers every module, incl. ones that never mention the
    authority; per-file copies of this fixture are deliberately gone (one origin).
    """
    from backend.app.services.plate_occupancy import plate_occupancy

    plate_occupancy.reset_for_tests()
    yield
    plate_occupancy.reset_for_tests()


@pytest.fixture(autouse=True)
def reset_dispatch_claim_registry():
    """Start and leave every test with an EMPTY start-watchdog registry.

    ``dispatch_claim._start_watchdogs`` is a process singleton keyed by queue-item id,
    and queue ids restart at 1 in every file's fresh DB — so a watchdog task registered
    by one module (and left un-popped, because its done-callback never ran on that
    file's closed loop) answers "this dispatch is owned" for a LATER module's item 1 and
    silently vetoes every dead-claim release in it. Same class as the two resets above,
    and for the same reason it lives here rather than per file.
    """
    from backend.app.services import dispatch_claim

    dispatch_claim._reset_state()
    yield
    dispatch_claim._reset_state()


def _reset_printer_manager(pm):
    """Drop every per-printer-id record the manager singleton carries."""
    pm.disconnect_all(timeout=0)
    pm._quarantined.clear()
    pm._model_mismatch.clear()
    pm._current_print_user.clear()
    pm._loop = None


@pytest.fixture(autouse=True)
def reset_printer_manager_process_state():
    """Start and leave every test with a bare ``printer_manager`` singleton.

    ``POST /api/v1/printers/`` registers a REAL ``BambuMQTTClient`` in the
    module-level manager, and nothing in the harness ever unregisters it.
    Printer ids restart at 1 in every file's fresh DB, so a client one file
    left behind answers a LATER file's lookup for the same id — and that
    client's full-report ``asyncio.Event`` is created once and reused forever,
    still bound to the loop of the file that made it. The USB pre-flight then
    awaits a dead loop's Event and raises ``RuntimeError: <asyncio.locks.Event>
    is bound to a different event loop`` — the ``test_capability_gate_api``
    failure that only ever appeared in multi-file runs. Product code is right
    (prod is one process, one loop, stable ids); this is harness hygiene, under
    the same rule as ``reset_plate_occupancy_authority`` above.

    Covers the singleton's per-printer-id process state: the
    client/model/printer-info triple (through the canonical ``disconnect_all``),
    the quarantine and model-mismatch caches, the current-print-user map, and
    the cached loop — a leaked dead loop makes ``_schedule_async`` skip
    silently. The nine ``_on_*`` callback slots are deliberately NOT reset:
    ``ASGITransport`` never runs the app lifespan, so nothing wires them under
    test, and clearing them here would break the first test that ever does
    drive a real lifespan.

    Teardown cost is real — paho's ``loop_stop()`` joins an untimed thread,
    ~5 s per client that was mid-connect. The before-leg is unconditional, so
    if a ``wired*`` fixture is ever made module-scoped this will wipe it
    between that module's tests.
    """
    from backend.app.services.printer_manager import printer_manager

    _reset_printer_manager(printer_manager)
    yield
    _reset_printer_manager(printer_manager)


@pytest.fixture(autouse=True)
def reset_spoolman_location_sync_cache():
    """Drop the per-URL Spoolman location-sync TTL cache between tests.

    Without this, a test that runs the sync against `http://localhost:7912`
    will skip the sync in any later test that uses the same URL within 60
    real seconds — test ordering would then leak assertions across runs."""
    from backend.app.services.location_service import _spoolman_location_sync_cache_clear

    _spoolman_location_sync_cache_clear()
    yield
    _spoolman_location_sync_cache_clear()


@pytest.fixture
async def async_client(test_engine, db_session, own_session_factory) -> AsyncGenerator[AsyncClient, None]:
    """Create an async test client."""
    from backend.app.core.database import async_session, get_db
    from backend.app.main import app

    # `own_session_factory` IS a sessionmaker over `test_engine` (see
    # _fixtures/db.py). Building a second one here would make conftest a rival
    # owner of "how a test session is made", which test_fixture_ownership pins
    # against -- and the two would drift the moment one gained an option.
    test_async_session = own_session_factory

    async def override_get_db():
        async with test_async_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Mock init_printer_connections to prevent MQTT connection attempts during tests
    async def mock_init_printer_connections(db):
        pass  # No-op - don't connect to real printers

    # Also patch the module-level async_session used by services, auth, and middleware
    with (
        patch("backend.app.core.database.async_session", test_async_session),
        patch("backend.app.core.auth.async_session", test_async_session),
        patch("backend.app.main.async_session", test_async_session),
        patch("backend.app.main.init_printer_connections", mock_init_printer_connections),
    ):
        # Seed default groups for tests that need them
        from backend.app.core.database import seed_default_groups

        await seed_default_groups()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client

        # The app lifespan called init_db() which used the module-level engine
        # (not the test engine), creating aiosqlite connections. Dispose those
        # connections so their background threads finish before the event loop closes.
        from backend.app.core.database import engine as real_engine

        await real_engine.dispose()

    app.dependency_overrides.clear()


# ============================================================================
# Mock External Services
# ============================================================================


@pytest.fixture
def mock_tasmota_service():
    """Mock the Tasmota service for smart plug tests."""
    # Patch both the module where it's defined and where it's imported
    with (
        patch("backend.app.services.tasmota.tasmota_service") as mock,
        patch("backend.app.api.routes.smart_plugs.tasmota_service") as mock2,
    ):
        mock.turn_on = AsyncMock(return_value=True)
        mock.turn_off = AsyncMock(return_value=True)
        mock.toggle = AsyncMock(return_value=True)
        mock.get_status = AsyncMock(return_value={"state": "ON", "reachable": True, "device_name": "Test Plug"})
        mock.get_energy = AsyncMock(
            return_value={
                "power": 150.5,
                "voltage": 120.0,
                "current": 1.25,
                "today": 2.5,
                "total": 100.0,
                "factor": 0.95,
            }
        )
        mock.test_connection = AsyncMock(return_value={"success": True, "state": "ON", "device_name": "Test Plug"})
        # Copy mocks to second patch target
        mock2.turn_on = mock.turn_on
        mock2.turn_off = mock.turn_off
        mock2.toggle = mock.toggle
        mock2.get_status = mock.get_status
        mock2.get_energy = mock.get_energy
        mock2.test_connection = mock.test_connection
        yield mock


@pytest.fixture
def mock_homeassistant_service():
    """Mock the Home Assistant service for smart plug tests."""
    # Patch both the module where it's defined and where it's imported
    with (
        patch("backend.app.services.homeassistant.homeassistant_service") as mock,
        patch("backend.app.api.routes.smart_plugs.homeassistant_service") as mock2,
    ):
        mock.turn_on = AsyncMock(return_value=True)
        mock.turn_off = AsyncMock(return_value=True)
        mock.toggle = AsyncMock(return_value=True)
        mock.get_status = AsyncMock(return_value={"state": "ON", "reachable": True, "device_name": "Test HA Entity"})
        mock.get_energy = AsyncMock(return_value=None)  # Most HA entities don't have power monitoring
        mock.test_connection = AsyncMock(return_value={"success": True, "message": "API running", "error": None})
        mock.list_entities = AsyncMock(
            return_value=[
                {
                    "entity_id": "switch.printer_plug",
                    "friendly_name": "Printer Plug",
                    "state": "on",
                    "domain": "switch",
                },
                {"entity_id": "switch.test", "friendly_name": "Test Switch", "state": "off", "domain": "switch"},
            ]
        )
        mock.configure = MagicMock()
        # Copy mocks to second patch target
        mock2.turn_on = mock.turn_on
        mock2.turn_off = mock.turn_off
        mock2.toggle = mock.toggle
        mock2.get_status = mock.get_status
        mock2.get_energy = mock.get_energy
        mock2.test_connection = mock.test_connection
        mock2.list_entities = mock.list_entities
        mock2.configure = mock.configure
        yield mock


@pytest.fixture
def mock_mqtt_smart_plug_service():
    """Mock the MQTT smart plug service for MQTT plug tests."""
    with patch("backend.app.api.routes.smart_plugs.mqtt_relay") as mock:
        # Create a mock smart_plug_service
        mock_service = MagicMock()
        mock_service.is_configured = MagicMock(return_value=True)
        mock_service.has_broker_settings = MagicMock(return_value=True)
        mock_service.configure = AsyncMock(return_value=True)
        mock_service.subscribe = MagicMock()
        mock_service.unsubscribe = MagicMock()
        mock_service.get_plug_data = MagicMock(return_value=None)
        mock_service.is_reachable = MagicMock(return_value=False)

        mock.smart_plug_service = mock_service
        yield mock


@pytest.fixture
def mock_httpx_client():
    """Mock httpx for webhook/notification HTTP calls."""
    with patch("httpx.AsyncClient") as mock_class:
        mock_instance = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "OK"
        mock_response.json.return_value = {}

        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_instance.post = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock()

        mock_class.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def existing_3mf_path():
    """`core.config.settings` patched so ``base_dir / <anything>`` is a path that exists.

    The usage and cost trackers resolve an archive's 3MF as ``settings.base_dir /
    archive.file_path`` and skip the whole extraction when it does not exist, so
    every test that wants the extraction to run has to make that path answer
    True. Yields the path itself, for callers that hand it straight to a
    pre-resolved ``threemf_path=`` argument.
    """
    with patch("backend.app.core.config.settings") as mock_settings:
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_settings.base_dir = MagicMock()
        mock_settings.base_dir.__truediv__ = MagicMock(return_value=mock_path)
        yield mock_path


@pytest.fixture
def mock_printer_manager():
    """Mock the printer manager for status checks."""
    with patch("backend.app.services.printer_manager.printer_manager") as mock:
        mock.get_status = MagicMock(
            return_value=MagicMock(
                connected=True,
                state="IDLE",
                progress=0,
                temperatures={"nozzle": 25, "bed": 25, "chamber": 25},
                raw_data={},
            )
        )
        mock.mark_printer_offline = MagicMock()
        yield mock


# ============================================================================
# Factory Fixtures for Test Data
# ============================================================================


@pytest.fixture
def smart_plug_factory(db_session):
    """Factory to create test smart plugs."""

    async def _create_plug(**kwargs):
        from backend.app.models.smart_plug import SmartPlug

        # Determine defaults based on plug_type
        plug_type = kwargs.get("plug_type", "tasmota")

        defaults = {
            "name": "Test Plug",
            "plug_type": plug_type,
            "enabled": True,
            "auto_on": True,
            "auto_off": True,
            "off_delay_mode": "time",
            "off_delay_minutes": 5,
            "off_temp_threshold": 70,
            "schedule_enabled": False,
            "power_alert_enabled": False,
        }

        # Set required fields based on plug_type
        if plug_type == "homeassistant":
            defaults["ha_entity_id"] = "switch.test"
            defaults["ip_address"] = None
        elif plug_type == "mqtt":
            # Legacy fields (for backward compatibility tests)
            defaults["mqtt_topic"] = kwargs.get("mqtt_topic", "test/topic")
            defaults["mqtt_multiplier"] = kwargs.get("mqtt_multiplier", 1.0)
            # New separate topic/path/multiplier fields
            defaults["mqtt_power_topic"] = kwargs.get("mqtt_power_topic")
            defaults["mqtt_power_path"] = kwargs.get("mqtt_power_path", "power")
            defaults["mqtt_power_multiplier"] = kwargs.get("mqtt_power_multiplier", 1.0)
            defaults["mqtt_energy_topic"] = kwargs.get("mqtt_energy_topic")
            defaults["mqtt_energy_path"] = kwargs.get("mqtt_energy_path")
            defaults["mqtt_energy_multiplier"] = kwargs.get("mqtt_energy_multiplier", 1.0)
            defaults["mqtt_state_topic"] = kwargs.get("mqtt_state_topic")
            defaults["mqtt_state_path"] = kwargs.get("mqtt_state_path")
            defaults["mqtt_state_on_value"] = kwargs.get("mqtt_state_on_value")
            defaults["ip_address"] = None
            defaults["ha_entity_id"] = None
        elif plug_type == "rest":
            defaults["rest_on_url"] = kwargs.get("rest_on_url", "http://192.168.1.100/api/plug/on")
            defaults["rest_off_url"] = kwargs.get("rest_off_url", "http://192.168.1.100/api/plug/off")
            defaults["rest_method"] = kwargs.get("rest_method", "POST")
            defaults["ip_address"] = None
            defaults["ha_entity_id"] = None
        else:
            defaults["ip_address"] = "192.168.1.100"
            defaults["ha_entity_id"] = None

        defaults.update(kwargs)

        plug = SmartPlug(**defaults)
        db_session.add(plug)
        await db_session.commit()
        await db_session.refresh(plug)
        return plug

    return _create_plug


@pytest.fixture
def printer_factory(db_session):
    """Factory to create test printers."""
    _counter = [0]  # Use list to allow mutation in nested function

    async def _create_printer(**kwargs):
        from backend.app.models.printer import Printer

        _counter[0] += 1
        counter = _counter[0]

        defaults = {
            "name": "Test Printer",
            "serial_number": f"00M09A{counter:09d}",  # Unique serial per printer
            "ip_address": f"192.168.1.{100 + counter}",  # Unique IP per printer
            "access_code": "12345678",
            "is_active": True,
            "auto_archive": True,
            "model": "X1C",
        }
        defaults.update(kwargs)

        printer = Printer(**defaults)
        db_session.add(printer)
        await db_session.commit()
        await db_session.refresh(printer)
        return printer

    return _create_printer


@pytest.fixture
def notification_provider_factory(db_session):
    """Factory to create test notification providers."""

    async def _create_provider(**kwargs):
        from backend.app.models.notification import NotificationProvider

        config = kwargs.pop("config", {"server": "https://ntfy.sh", "topic": "test-topic"})
        if isinstance(config, dict):
            config = json.dumps(config)

        defaults = {
            "name": "Test Provider",
            "provider_type": "ntfy",
            "enabled": True,
            "config": config,
            "on_print_start": True,
            "on_print_complete": True,
            "on_print_failed": True,
            "on_print_stopped": True,
            "on_print_progress": False,
            "on_print_missing_spool_assignment": False,
            "on_printer_offline": False,
            "on_printer_error": False,
            "on_filament_low": False,
            "on_maintenance_due": False,
            "on_ams_humidity_high": False,
            "on_ams_temperature_high": False,
            "on_bed_cooled": False,
            "quiet_hours_enabled": False,
            "daily_digest_enabled": False,
        }
        defaults.update(kwargs)

        provider = NotificationProvider(**defaults)
        db_session.add(provider)
        await db_session.commit()
        await db_session.refresh(provider)
        return provider

    return _create_provider


@pytest.fixture
def archive_factory(db_session):
    """Factory to create test archives.

    Also synthesizes one PrintLogEntry per archive (matching the production
    flow where statistics are aggregated from PrintLogEntry, not PrintArchive,
    per #1378). Pass ``with_run=False`` to skip — useful for testing the
    "archived but never printed" state. Pass ``run_status=...`` to override
    the run's status independently of the archive's status field.
    """

    async def _create_archive(printer_id: int, **kwargs):
        from backend.app.models.archive import PrintArchive
        from backend.app.models.print_log import PrintLogEntry

        with_run = kwargs.pop("with_run", True)
        run_status = kwargs.pop("run_status", None)

        defaults = {
            "printer_id": printer_id,
            "filename": "test_print.gcode.3mf",
            "print_name": "Test Print",
            "file_path": "archives/test/test_print.gcode.3mf",
            "file_size": 1024000,
            "status": "completed",
            "filament_type": "PLA",
            "filament_used_grams": 50.0,
            "print_time_seconds": 3600,
        }
        defaults.update(kwargs)

        archive = PrintArchive(**defaults)
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)

        if with_run:
            duration = None
            if archive.started_at and archive.completed_at:
                duration = int((archive.completed_at - archive.started_at).total_seconds()) or None
            run = PrintLogEntry(
                archive_id=archive.id,
                printer_id=archive.printer_id,
                status=run_status or archive.status,
                started_at=archive.started_at,
                completed_at=archive.completed_at,
                duration_seconds=duration,
                filament_type=archive.filament_type,
                filament_color=archive.filament_color,
                filament_used_grams=archive.filament_used_grams,
                cost=archive.cost,
                energy_kwh=archive.energy_kwh,
                energy_cost=archive.energy_cost,
                failure_reason=archive.failure_reason,
                print_name=archive.print_name,
                created_by_id=archive.created_by_id,
                # Sync the event's created_at with the archive's so date-range
                # filtered tests that backdate an archive still find its event.
                created_at=archive.created_at,
            )
            db_session.add(run)
            await db_session.commit()

        return archive

    return _create_archive


# ============================================================================
# Log Capture Fixtures for Error Detection
# ============================================================================


class LogCapture(logging.Handler):
    """Handler that captures log records for testing."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)

    def clear(self):
        self.records.clear()

    def get_errors(self) -> list[logging.LogRecord]:
        """Get all ERROR and CRITICAL level records."""
        return [r for r in self.records if r.levelno >= logging.ERROR]

    def get_warnings(self) -> list[logging.LogRecord]:
        """Get all WARNING level records."""
        return [r for r in self.records if r.levelno == logging.WARNING]

    def has_errors(self) -> bool:
        """Check if any errors were logged."""
        return len(self.get_errors()) > 0

    def format_errors(self) -> str:
        """Format all errors as a string for assertion messages."""
        errors = self.get_errors()
        if not errors:
            return "No errors"
        formatter = logging.Formatter("%(name)s - %(levelname)s - %(message)s")
        return "\n".join(formatter.format(r) for r in errors)


@pytest.fixture
def capture_logs():
    """Fixture that captures log output during a test.

    Usage:
        def test_something(capture_logs):
            # Do something that might log errors
            some_function()

            # Check no errors were logged
            assert not capture_logs.has_errors(), capture_logs.format_errors()
    """
    handler = LogCapture()
    handler.setLevel(logging.DEBUG)

    # Attach to root logger to capture all logs
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    yield handler

    root_logger.removeHandler(handler)
