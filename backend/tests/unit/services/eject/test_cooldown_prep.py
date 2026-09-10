"""Tests for the cooldown prep — the plate hold + aux fan run during the eject wait.

Everything here drives the REAL :func:`cooldown_prep.begin` / :meth:`CooldownPrep.end`
against a spy MQTT client and a stubbed printer manager, with the G-code generator
left REAL: the one thing a test must never do is re-implement the block it is pinning,
because then a change to the hold's shape passes both sides at once.

The house rule this suite exists to hold: ``begin`` never raises and never leaves the
cooldown without a watch. Every failure it can meet is one ``skipped:*`` outcome, a
log line, and a cooldown that proceeds — so almost every test below asserts an
OUTCOME plus "and the actuator was not commanded", never an exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import cooldown_prep, generator
from backend.app.services.eject.geometry import GeometryUnavailable

from .geometry_fixtures import H2S_GEOMETRY

# The production shape of the two seed-only registry numbers (H2S, 2026-09-10):
# the toolhead's chute keep-out line and the clear height above the nozzle plane.
HOLD_GEOMETRY = replace(
    H2S_GEOMETRY,
    max_part_height_mm=55.0,
    cooldown_hold_keepout_y_mm=285.0,
    cooldown_hold_clear_above_mm=51.0,
)
# A model whose registry row carries neither number — every model but H2S today.
FAN_ONLY_GEOMETRY = H2S_GEOMETRY
# A bedslinger with both numbers set, so the bedslinger gate is what refuses it and
# not the missing pair (A2L: the bed moves in Y and the GANTRY carries Z).
BEDSLINGER_GEOMETRY = replace(HOLD_GEOMETRY, model_key="A2L", z_travel_mm=None)

PRINTER_ID = 7
ITEM_ID = 42
# A production part height and the plate footprint that goes with it (bbox_all ends
# at Y262 on the production corpus, well clear of the 285 keep-out line).
MAX_Z = 50.1
BBOX = ((10.0, 20.0, 300.0, 262.0), 1)


def _profile(**overrides) -> EjectProfile:
    defaults = {
        "name": "single-pass-quarter-height",
        "cooldown_temp_c": 33.0,
        "clearance_mm": 10.0,
        "z_offset_mm": 0.4,
        "descent_steps": 4,
        "x_passes": 11,
        "x_margin_mm": 3.0,
        "front_overhang_mm": 2.0,
        "back_overhang_mm": 2.0,
        "eject_speed_mm_min": 3000,
        "skim_speed_mm_min": 1500,
        "cooling_fan_assist": True,
        "max_part_height_mm": 55.0,
    }
    defaults.update(overrides)
    return EjectProfile(**defaults)


class _SpyClient:
    """The MQTT client, recording what was published and answering how it was told to."""

    def __init__(self, *, gcode_ok: bool = True, fan_ok: bool = True) -> None:
        self.gcode: list[str] = []
        self.fans: list[tuple[int, int]] = []
        self.gcode_ok = gcode_ok
        self.fan_ok = fan_ok

    def send_gcode(self, text: str) -> bool:
        self.gcode.append(text)
        return self.gcode_ok

    def set_fan_percent(self, fan: int, percent: int) -> bool:
        self.fans.append((fan, percent))
        return self.fan_ok


class _FakeManager:
    """``printer_manager`` reduced to the three questions this module asks it."""

    def __init__(self, client: _SpyClient | None) -> None:
        self.client = client
        self.connected = True
        self.state: str | None = "FINISH"
        self.fan: int | None = None

    def get_client(self, printer_id: int) -> _SpyClient | None:
        return self.client

    def is_connected(self, printer_id: int) -> bool:
        return self.connected

    def get_status(self, printer_id: int):
        return SimpleNamespace(state=self.state, big_fan1_speed=self.fan)


class _FakeDb:
    """``db.get(Model, pk)`` over a per-model row table."""

    def __init__(self, rows: dict[type, object | None]) -> None:
        self.rows = rows

    async def get(self, model: type, pk):
        return self.rows.get(model)


class _Env:
    """A working happy path with every collaborator stubbed; each test mutates one fact.

    Assembling it once is what keeps the failure tests honest — a test that says
    ``skipped:donor`` has changed exactly the donor and nothing else, so the outcome
    it asserts cannot be produced by an unrelated stub that was never set up.
    """

    def __init__(self, monkeypatch) -> None:
        self.client = _SpyClient()
        self.manager = _FakeManager(self.client)
        self.geometry = HOLD_GEOMETRY
        self.geometry_error: GeometryUnavailable | None = None
        self.item = PrintQueueItem(eject_profile_id=5, plate_id=None)
        self.profile = _profile()
        self.printer = Printer(name="001-H2S", model="H2S")
        self.donor = SimpleNamespace(
            local_path=Path("donor.gcode.3mf"), filename="unit.gcode.3mf", plate_id=1, item_id=ITEM_ID
        )
        self.max_z: float | None = MAX_Z
        self.bbox = BBOX
        self.refusal: str | None = None
        self.sessions = 0
        self.sleeps: list[float] = []
        self.plate_ids: list[int] = []
        self.db_error: Exception | None = None

        env = self

        @contextlib.asynccontextmanager
        async def fake_session():
            env.sessions += 1
            if env.db_error is not None:
                raise env.db_error
            yield _FakeDb({PrintQueueItem: env.item, EjectProfile: env.profile, Printer: env.printer})

        async def fake_geometry(db, model, *, require_validated):
            assert require_validated is True  # production gate: never an unvalidated envelope
            if env.geometry_error is not None:
                raise env.geometry_error
            return env.geometry

        async def fake_donor(db, item):
            return env.donor

        def fake_max_z(path, plate_id):
            env.plate_ids.append(plate_id)
            return env.max_z

        def fake_bbox(path, plate_id):
            return env.bbox

        import backend.app.services.farm_correlation as farm_correlation

        monkeypatch.setattr("backend.app.core.database.async_session", fake_session, raising=False)
        monkeypatch.setattr(cooldown_prep, "printer_manager", self.manager)
        monkeypatch.setattr(cooldown_prep, "get_geometry_required", fake_geometry)
        monkeypatch.setattr(cooldown_prep.donor, "read_max_z", fake_max_z)
        monkeypatch.setattr(cooldown_prep.donor, "read_plate_bbox", fake_bbox)
        monkeypatch.setattr(farm_correlation, "resolve_item_donor", fake_donor)
        monkeypatch.setattr(cooldown_prep, "plate_occupancy", SimpleNamespace(ejectable=lambda pid, ev: env.refusal))
        monkeypatch.setattr(cooldown_prep, "eject_remote", SimpleNamespace(_live_evidence=lambda pid: "EV"))

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def begin(self, *, queue_item_id: int | None = ITEM_ID, aux_fan_percent: int = 100, settle_s: float = 3.0):
        """``begin`` followed by the arm-time witness, the way ``_watch`` drives them."""
        prep = await cooldown_prep.begin(
            PRINTER_ID,
            queue_item_id=queue_item_id,
            aux_fan_percent=aux_fan_percent,
            settle_s=settle_s,
            sleep=self.sleep,
        )
        await prep.observe_start()
        return prep


@pytest.fixture
def env(monkeypatch) -> _Env:
    return _Env(monkeypatch)


def _summary(caplog) -> str:
    """The ONE end-of-cooldown line — the record the whole wave is measured by."""
    lines = [r.getMessage() for r in caplog.records if "cooldown ended after" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one summary line, got {lines}"
    return lines[0]


# --------------------------------------------------------------------------- #
# The plate hold
# --------------------------------------------------------------------------- #
class TestPlateHold:
    async def test_sends_the_generator_block_and_reports_its_hold_z(self, env, caplog):
        """The happy path: the published text IS the generator's block, verbatim.

        Pinned against a live call to ``cooldown_hold_lines`` rather than a literal —
        the coordinates are the generator's to own, and a test that re-typed them
        would go on passing after the block changed."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()

        assert prep.hold == "sent"
        assert prep.hold_z == 2.0  # a 50.1 mm part under 51 mm of clearance
        assert prep.max_z == MAX_Z
        expected = "\n".join(generator.cooldown_hold_lines(MAX_Z, env.profile, 51.0))
        assert env.client.gcode == [expected]
        assert "plate hold sent" in "\n".join(r.getMessage() for r in caplog.records)

    async def test_hold_z_tracks_the_part_height(self, env):
        """55 mm under the same 51 mm of clearance holds 2 mm lower than the floor case."""
        env.max_z = 55.0
        prep = await env.begin()
        assert prep.hold == "sent"
        assert prep.hold_z == 4.0

    async def test_plate_id_precedence_is_the_dispatchers(self, env):
        """The FARM's dispatched plate first, the unit's own next, 1 last — the same
        precedence ``remote.dispatch_part_present_eject`` uses."""
        env.donor.plate_id = 2
        env.item.plate_id = 3
        await env.begin()
        env.donor.plate_id = None
        await env.begin()
        env.item.plate_id = None
        await env.begin()
        assert env.plate_ids == [2, 3, 1]

    async def test_foreign_watch_never_holds_and_never_opens_a_session(self, env):
        """No unit ⇒ no donor, no profile, no measured height: nothing to hold safely."""
        prep = await env.begin(queue_item_id=None)
        assert prep.hold == "skipped:foreign"
        assert prep.hold_z is None
        assert env.sessions == 0  # the refusal is decided before any DB work
        assert env.client.gcode == []
        assert env.client.fans == [(2, 100)]  # the fan still runs on a foreign plate

    async def test_missing_item(self, env):
        env.item = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:item_missing", None)
        assert env.client.gcode == []

    async def test_missing_profile(self, env):
        env.profile = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:profile_missing", None)

    async def test_unit_with_no_profile_id_never_looks_one_up(self, env):
        env.item = PrintQueueItem(eject_profile_id=None)
        prep = await env.begin()
        assert prep.hold == "skipped:profile_missing"

    async def test_geometry_unavailable(self, env):
        env.geometry_error = GeometryUnavailable("geometry for 'H2S' is not hardware-validated")
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:geometry", None)
        assert env.client.gcode == []

    async def test_model_without_registry_numbers_is_fan_only(self, env):
        """H2C today: the pair is NULL until its clearance is measured.

        A one-sided row cannot be constructed at all (``ModelGeometry.__post_init__``
        refuses it), so both-None is the only shape this gate can meet in production —
        the code still tests both halves, fail-closed."""
        env.geometry = FAN_ONLY_GEOMETRY
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:no_numbers", None)
        assert env.client.gcode == []
        assert env.client.fans == [(2, 100)]  # fan-only is a WORKING cooldown prep

    async def test_bedslinger_never_holds(self, env):
        """On a bedslinger, raising the plate toward the nozzle plane IS lowering the
        nozzle onto the part."""
        env.geometry = BEDSLINGER_GEOMETRY
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:bedslinger", None)
        assert env.client.gcode == []

    async def test_unresolvable_donor(self, env):
        env.donor = None
        prep = await env.begin()
        assert (prep.hold, prep.max_z) == ("skipped:donor", None)

    async def test_plate_without_a_max_z_height(self, env):
        env.max_z = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z, prep.max_z) == ("skipped:max_z", None, None)

    async def test_over_height_part_refuses_with_the_generators_own_sentence(self, env, caplog):
        """A part taller than the profile's guard cannot eject, so it must not be held
        near the nozzle either."""
        env.max_z = 60.0
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:over_height", None)
        assert prep.max_z == 60.0  # measured, then refused — the two are distinguishable
        assert "exceeds profile max_part_height_mm" in caplog.text
        assert env.client.gcode == []


class TestKeepOut:
    """The chute keep-out gate: hold only when the plate is PROVABLY clear of the
    strip the parked toolhead occupies. All three refusals are fail-closed."""

    async def test_unreadable_bbox_never_holds(self, env):
        env.bbox = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:keepout", None)
        assert prep.max_z == MAX_Z

    @pytest.mark.parametrize("n_filaments", [0, 2, 4])
    async def test_only_a_single_filament_plate_is_trusted(self, env, n_filaments):
        """``bbox_all`` covers OBJECTS only — a multi-filament plate puts a purge tower
        on the bed that the rectangle does not describe, and 0 means UNKNOWN."""
        env.bbox = ((10.0, 20.0, 300.0, 262.0), n_filaments)
        prep = await env.begin()
        assert prep.hold == "skipped:keepout"

    async def test_holds_at_the_keepout_boundary(self, env):
        env.bbox = ((10.0, 20.0, 300.0, 285.0), 1)  # exactly ON the line
        prep = await env.begin()
        assert prep.hold == "sent"

    async def test_refuses_just_past_the_keepout_boundary(self, env, caplog):
        env.bbox = ((10.0, 20.0, 300.0, 285.1), 1)
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:keepout", None)
        assert "keep-out" in caplog.text and "285.10" in caplog.text
        assert env.client.gcode == []


class TestHoldAdmission:
    """The hold moves the same machine an eject does, so it asks the eject's own
    admission pair — and asks it as LATE as possible, right before the publish."""

    async def test_disconnected_printer(self, env):
        env.manager.connected = False
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:disconnected", None)
        assert env.client.gcode == []

    @pytest.mark.parametrize("refusal", ["z_unreferenced", "job_active"])
    async def test_ejectable_refusals_hold_nothing(self, env, refusal, caplog):
        """A printer that may not be swept may not be moved: a lost Z datum makes every
        absolute Z a hazard, and a live job owns the machine."""
        env.refusal = refusal
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:not_ejectable", None)
        assert refusal in caplog.text  # the refusal token is named, not swallowed
        assert env.client.gcode == []

    async def test_refused_publish(self, env):
        env.client.gcode_ok = False
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:publish", None)
        assert len(env.client.gcode) == 1  # attempted exactly once, never retried

    async def test_no_client_at_all(self, env):
        env.manager.client = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:publish", None)


class TestBeginNeverRaises:
    async def test_db_failure_is_an_outcome_not_an_exception(self, env, caplog):
        """A dead settings/queue DB must not kill the watch that owns the plate gate —
        and must not cost the cooldown its fan either."""
        env.db_error = RuntimeError("queue DB unavailable")
        with caplog.at_level(logging.ERROR, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:error", None)
        assert "plate hold failed" in caplog.text
        assert env.client.fans == [(2, 100)]  # the fan is armed regardless
        assert prep.fan_published is True

    async def test_fan_failure_is_an_outcome_not_an_exception(self, env):
        """A publish that throws leaves ``fan_published`` False and the hold intact."""

        def boom(fan, percent):
            raise RuntimeError("MQTT down")

        env.client.set_fan_percent = boom
        prep = await env.begin()
        assert prep.hold == "sent"
        assert prep.fan_published is False


# --------------------------------------------------------------------------- #
# The auxiliary fan
# --------------------------------------------------------------------------- #
class TestAuxFan:
    async def test_runs_the_aux_fan_at_the_configured_percent(self, env):
        prep = await env.begin(aux_fan_percent=100)
        assert env.client.fans == [(2, 100)]  # fan 2 is the auxiliary duct
        assert prep.fan_published is True
        assert prep.fan_percent == 100

    async def test_zero_percent_commands_nothing(self, env, caplog):
        """0 = the operator switched the aux fan off fleet-wide."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(aux_fan_percent=0)
        assert env.client.fans == []
        assert prep.fan_published is False
        assert env.sleeps == []  # no witness wait for a command that was never sent
        assert prep.hold == "sent"  # the hold is independent of the fan

    @pytest.mark.parametrize("state", ["RUNNING", "PREPARE", "PAUSE", "SLICING"])
    async def test_never_commands_the_fan_while_a_job_is_active(self, env, state):
        env.manager.state = state
        prep = await env.begin()
        assert env.client.fans == []
        assert prep.fan_published is False

    async def test_unreadable_state_is_not_active(self, env):
        """A printer whose state cannot be read is not a printer running a job —
        an idle machine is safe for both actuators."""
        env.manager.state = None
        prep = await env.begin()
        assert env.client.fans == [(2, 100)]
        assert prep.fan_published is True

    async def test_refused_fan_publish(self, env):
        env.client.fan_ok = False
        prep = await env.begin()
        assert prep.fan_published is False
        assert env.sleeps == []

    async def test_missing_client(self, env):
        env.manager.client = None
        prep = await env.begin()
        assert prep.fan_published is False

    async def test_witness_is_read_after_the_settle_wait(self, env, caplog):
        """The arm-time witness must be sampled AFTER the wire has had time to report
        the change — a read taken before the settle would witness the old value."""

        async def sleep(seconds):
            env.sleeps.append(seconds)
            env.manager.fan = 100  # the push lands during the wait

        env.sleep = sleep
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await env.begin(settle_s=1.5)
        assert env.sleeps == [1.5]
        assert "fan_observed_at_start=100" in caplog.text

    async def test_begin_returns_before_the_settle_wait(self, env, caplog):
        """No await may sit between the fan ON and the handle: a watch cancelled
        during the settle must already hold the prep so its ``finally`` can retire
        it. The witness therefore waits in ``observe_start``, not in ``begin``."""
        prep = await cooldown_prep.begin(
            PRINTER_ID, queue_item_id=ITEM_ID, aux_fan_percent=100, settle_s=1.5, sleep=env.sleep
        )
        assert env.client.fans == [(2, 100)]
        assert prep.fan_published is True
        assert env.sleeps == []  # begin itself never waited
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await prep.observe_start()
        assert env.sleeps == [1.5]
        assert "fan_observed_at_start=" in caplog.text

    async def test_observe_start_is_a_no_op_when_nothing_was_published(self, env, caplog):
        prep = await cooldown_prep.begin(
            PRINTER_ID, queue_item_id=ITEM_ID, aux_fan_percent=0, settle_s=1.5, sleep=env.sleep
        )
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await prep.observe_start()
        assert env.sleeps == []
        assert "fan_observed_at_start=" not in caplog.text

    async def test_a_quantised_witness_is_logged_not_enforced(self, env, caplog):
        """The wire reports a 0-15 LEVEL rescaled to percent, so an observation
        legitimately differs from the commanded percent. It is evidence, never a
        confirmation: a mismatch changes NOTHING about the prep."""
        env.manager.fan = 67  # what a 70 % command comes back as
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(aux_fan_percent=70)
            prep.end(fan_off=False)
        assert prep.fan_published is True
        assert "fan_observed_at_start=67" in caplog.text
        assert "fan_observed=67" in _summary(caplog)


# --------------------------------------------------------------------------- #
# end(): the fan hand-over and the ONE summary line
# --------------------------------------------------------------------------- #
class TestEnd:
    async def test_switches_the_fan_off_when_nothing_wants_it(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
        assert env.client.fans == [(2, 100), (2, 0)]
        assert "fan_off=sent" in _summary(caplog)

    async def test_hands_the_fan_over_when_a_successor_wants_it(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=False)
        assert env.client.fans == [(2, 100)]  # left running for the next watch
        assert "fan_off=skipped:not_wanted" in _summary(caplog)

    async def test_never_switches_off_a_fan_it_did_not_start(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(aux_fan_percent=0)
            prep.end(fan_off=True)
        assert env.client.fans == []
        assert "fan_off=skipped:not_published" in _summary(caplog)

    async def test_never_switches_off_during_the_eject_it_released_into(self, env, caplog):
        """The normal release exit: the sweep is already running and carries its own
        ``M106 P2 S0``. Commanding one here would race that job."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.manager.state = "RUNNING"
            prep.end(fan_off=True)
        assert env.client.fans == [(2, 100)]
        assert "fan_off=skipped:active" in _summary(caplog)

    async def test_missing_client_at_exit(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.manager.client = None
            prep.end(fan_off=True)
        assert "fan_off=skipped:no_client" in _summary(caplog)

    async def test_refused_off_publish(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.client.fan_ok = False
            prep.end(fan_off=True)
        assert "fan_off=skipped:publish" in _summary(caplog)

    async def test_end_never_raises(self, env, caplog):
        def boom(fan, percent):
            raise RuntimeError("MQTT down")

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.client.set_fan_percent = boom
            prep.end(fan_off=True)  # must not propagate — it runs in the watch's finally
        assert "fan_off=skipped:publish" in _summary(caplog)

    async def test_summary_line_shape_when_the_plate_was_held(self, env, caplog):
        """The ONE greppable line the wave is measured by, pinned whole."""
        env.manager.fan = 100
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
        assert _summary(caplog) == (
            "[cooldown-prep] printer 7: cooldown ended after 0 s "
            "(hold=sent max_z=50.10 hold_z=2.00, fan=100%, fan_published=True, "
            "fan_off=sent, fan_observed=100)"
        )

    async def test_summary_line_shape_when_the_hold_was_skipped(self, env, caplog):
        """Same shape on the fan-only path — one line whatever happened, so the
        per-printer medians are comparable across H2S and H2C."""
        env.geometry = FAN_ONLY_GEOMETRY
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
        assert _summary(caplog) == (
            "[cooldown-prep] printer 7: cooldown ended after 0 s "
            "(hold=skipped:no_numbers max_z=none hold_z=none, fan=100%, fan_published=True, "
            "fan_off=sent, fan_observed=none)"
        )

    async def test_summary_line_shape_with_the_fan_disabled(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(aux_fan_percent=0)
            prep.end(fan_off=True)
        assert _summary(caplog) == (
            "[cooldown-prep] printer 7: cooldown ended after 0 s "
            "(hold=sent max_z=50.10 hold_z=2.00, fan=0%, fan_published=False, "
            "fan_off=skipped:not_published, fan_observed=none)"
        )


class TestHoldZIsOnlyEverSetOnSent:
    """Every skip leaves ``hold_z`` None — the estimator seed may only come from a
    hold that actually happened, never from one that was merely computed."""

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda e: setattr(e, "geometry", FAN_ONLY_GEOMETRY), id="no_numbers"),
            pytest.param(lambda e: setattr(e, "geometry", BEDSLINGER_GEOMETRY), id="bedslinger"),
            pytest.param(lambda e: setattr(e, "donor", None), id="donor"),
            pytest.param(lambda e: setattr(e, "max_z", None), id="max_z"),
            pytest.param(lambda e: setattr(e, "bbox", None), id="keepout"),
            pytest.param(lambda e: setattr(e, "max_z", 60.0), id="over_height"),
            pytest.param(lambda e: setattr(e.manager, "connected", False), id="disconnected"),
            pytest.param(lambda e: setattr(e, "refusal", "job_active"), id="not_ejectable"),
            pytest.param(lambda e: setattr(e.client, "gcode_ok", False), id="publish"),
            pytest.param(lambda e: setattr(e, "item", None), id="item_missing"),
            pytest.param(lambda e: setattr(e, "profile", None), id="profile_missing"),
            pytest.param(lambda e: setattr(e, "db_error", RuntimeError("db")), id="error"),
        ],
    )
    async def test_skip_leaves_no_seed(self, env, mutate):
        mutate(env)
        prep = await env.begin()
        assert prep.hold != "sent"
        assert prep.hold_z is None


async def test_begin_runs_the_hold_before_the_fan(env):
    """Order matters: the hold wants the machine in exactly the idle state the end
    block left it, and nothing should sit between the terminal and that motion."""
    order: list[str] = []
    env.client.send_gcode = lambda text: (order.append("hold"), True)[1]
    env.client.set_fan_percent = lambda fan, pct: (order.append("fan"), True)[1]
    await env.begin()
    assert order == ["hold", "fan"]


async def test_settle_wait_defaults_to_the_event_loops_own_sleep(env):
    """``sleep``/``settle_s`` are injected for tests only — production waits on the
    real loop, which is why every test here passes its own."""
    import inspect

    signature = inspect.signature(cooldown_prep.begin)
    assert signature.parameters["sleep"].default is asyncio.sleep
    assert signature.parameters["settle_s"].default == 3.0
