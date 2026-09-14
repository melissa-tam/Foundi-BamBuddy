"""Tests for the cooldown prep — the plate hold and the two fans run during the eject wait.

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
from backend.app.schemas.settings import AppSettings
from backend.app.services.eject import cooldown_prep, generator
from backend.app.services.eject.cooldown_prep import AIRDUCT_COOLING_GCODE, CooldownFanSettings, FanRequest
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
# The operator's hold target, read from the schema so this suite cannot drift from it.
HOLD_PART_TOP_DEFAULT = int(AppSettings.model_fields["farm_cooldown_hold_part_top_mm"].default)
# The eject threshold the fleet runs (~2 °C above shop ambient) — and, since this wave,
# the line the chamber lane steps its boost down at.
THRESHOLD_C = 33.0
# The fleet's model: a chamber fan AND a switchable duct.
MODEL = "H2S"
# No chamber fan at all (the open-frame family).
MODEL_WITHOUT_CHAMBER_FAN = "A1"
# A chamber fan behind a FIXED duct — enclosed, but no flap to open. ``AIRDUCT_MODELS``
# is a strict subset of ``CHAMBER_FAN_MODELS``, and this is the difference.
MODEL_WITHOUT_AIRDUCT = "P1S"

_UNSET = object()


def _fans(
    *,
    aux_enabled: bool = True,
    aux_percent: int = 100,
    chamber_enabled: bool = True,
    chamber_boost: int = 100,
    chamber_sustain: int = 50,
) -> CooldownFanSettings:
    """The two lanes' requests, shaped the way ``CooldownWatchSettings.fans`` shapes them.

    The aux lane's sustain is ALWAYS its own speed — that lane has no step-down by
    construction and no setting offers one — so this helper cannot accidentally hand
    the aux fan a step the production composition site would never produce.
    """
    return CooldownFanSettings(
        aux=FanRequest(aux_enabled, aux_percent, aux_percent),
        chamber=FanRequest(chamber_enabled, chamber_boost, chamber_sustain),
    )


# The production defaults: both fans on, aux one speed, chamber 100 % boosting down to
# the vendor's own 50 % chamber-cooling figure.
FANS_ON = _fans()


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
        "max_part_height_mm": 55.0,
    }
    defaults.update(overrides)
    return EjectProfile(**defaults)


class _SpyClient:
    """The MQTT client, recording what was published and answering how it was told to.

    Three record keeping devices, because the module now publishes over two channels
    to three actuators:

    * ``gcode`` is every ``send_gcode`` text in order, and the ``hold_gcode`` /
      ``airduct`` views split it into the plate hold's block and the chamber lane's
      vendor prelude — so a hold assertion reads the hold and never the ``M145 P0``
      line that now shares the channel with it;
    * ``fans`` is every ``(index, percent)`` pair, which is what "the fan was not
      commanded" is asserted against; and
    * ``calls`` is the ONE interleaved ordering of both channels, which is the only
      way to pin that the duct opens BEFORE the exhaust fan spins.

    ``gcode_ok`` / ``airduct_ok`` answer the two G-code kinds separately, and
    ``refuse_fans`` refuses one fan index while the other still succeeds — the
    independence every "the other lane is intact" assertion rests on.
    """

    def __init__(self, *, gcode_ok: bool = True, fan_ok: bool = True, airduct_ok: bool = True) -> None:
        self.gcode: list[str] = []
        self.fans: list[tuple[int, int]] = []
        self.calls: list[str] = []
        self.gcode_ok = gcode_ok
        self.fan_ok = fan_ok
        self.airduct_ok = airduct_ok
        self.refuse_fans: set[int] = set()

    @property
    def hold_gcode(self) -> list[str]:
        """Every published G-code text that is NOT the chamber lane's duct prelude."""
        return [text for text in self.gcode if text != AIRDUCT_COOLING_GCODE]

    @property
    def airduct(self) -> list[str]:
        """Just the duct-to-cooling preludes."""
        return [text for text in self.gcode if text == AIRDUCT_COOLING_GCODE]

    def send_gcode(self, text: str) -> bool:
        self.gcode.append(text)
        if text == AIRDUCT_COOLING_GCODE:
            self.calls.append("gcode:M145 P0")
            return self.airduct_ok
        self.calls.append("hold")
        return self.gcode_ok

    def set_fan_percent(self, fan: int, percent: int) -> bool:
        self.fans.append((fan, percent))
        self.calls.append(f"fan:{fan}")
        return False if fan in self.refuse_fans else self.fan_ok


class _FakeManager:
    """``printer_manager`` reduced to the three questions this module asks it."""

    def __init__(self, client: _SpyClient | None) -> None:
        self.client = client
        self.connected = True
        self.state: str | None = "FINISH"
        # One witness field per fan, exactly as the wire reports them.
        self.fan: int | None = None  # big_fan1_speed — the auxiliary duct
        self.chamber_fan: int | None = None  # big_fan2_speed — the chamber exhaust

    def get_client(self, printer_id: int) -> _SpyClient | None:
        return self.client

    def is_connected(self, printer_id: int) -> bool:
        return self.connected

    def get_status(self, printer_id: int):
        return SimpleNamespace(state=self.state, big_fan1_speed=self.fan, big_fan2_speed=self.chamber_fan)


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
        # The operator's two hold inputs, resolved once by the watch and handed to
        # ``begin``. The defaults are the schema's: the hold is on, and the target asks
        # for as much of the clear zone as the part can reach.
        self.hold_enabled = True
        self.hold_part_top_mm = HOLD_PART_TOP_DEFAULT
        # The other three inputs the watch resolves once and hands in: both fans, the
        # printer's model (the chamber lane's capability gate) and the release
        # threshold (the chamber lane's step-down line).
        self.fans = FANS_ON
        self.model: str | None = MODEL
        self.release_threshold_c = THRESHOLD_C
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

    async def begin(
        self,
        *,
        queue_item_id: int | None = ITEM_ID,
        fans: CooldownFanSettings | None = None,
        model: str | None = _UNSET,  # type: ignore[assignment]
        release_threshold_c: float | None = None,
        held: bool = False,
        settle_s: float = 3.0,
    ):
        """``begin`` followed by the arm-time witness, the way ``_watch`` drives them."""
        prep = await cooldown_prep.begin(
            PRINTER_ID,
            queue_item_id=queue_item_id,
            fans=self.fans if fans is None else fans,
            release_threshold_c=self.release_threshold_c if release_threshold_c is None else release_threshold_c,
            model=self.model if model is _UNSET else model,
            hold_enabled=self.hold_enabled,
            hold_part_top_mm=self.hold_part_top_mm,
            held=held,
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


def _boost_lines(caplog) -> list[str]:
    """Every ``chamber boost ended`` line — THE measurement this wave exists to take."""
    return [r.getMessage() for r in caplog.records if "chamber boost ended" in r.getMessage()]


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
        expected = "\n".join(generator.cooldown_hold_lines(MAX_Z, env.profile, 51.0, HOLD_PART_TOP_DEFAULT)[0])
        assert env.client.hold_gcode == [expected]
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
        assert env.client.hold_gcode == []
        assert env.client.fans == [(2, 100), (3, 100)]  # the fans still run on a foreign plate

    async def test_missing_item(self, env):
        env.item = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:item_missing", None)
        assert env.client.hold_gcode == []

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
        assert env.client.hold_gcode == []

    async def test_model_without_registry_numbers_is_fan_only(self, env):
        """H2C today: the pair is NULL until its clearance is measured.

        A one-sided row cannot be constructed at all (``ModelGeometry.__post_init__``
        refuses it), so both-None is the only shape this gate can meet in production —
        the code still tests both halves, fail-closed."""
        env.geometry = FAN_ONLY_GEOMETRY
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:no_numbers", None)
        assert env.client.hold_gcode == []
        assert env.client.fans == [(2, 100), (3, 100)]  # fan-only is a WORKING cooldown prep

    async def test_bedslinger_never_holds(self, env):
        """On a bedslinger, raising the plate toward the nozzle plane IS lowering the
        nozzle onto the part."""
        env.geometry = BEDSLINGER_GEOMETRY
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:bedslinger", None)
        assert env.client.hold_gcode == []

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
        assert env.client.hold_gcode == []


class TestTheOperatorsHoldSettings:
    """The switch and the target: two numbers the watch resolves and hands in.

    Nothing here is re-read from the settings store — the prep computes no height and
    reads no setting, so what these pin is that the operator's inputs travel intact from
    ``begin`` to the emitted G-code and to the record the estimator is seeded from."""

    async def test_the_switch_off_skips_the_hold_before_any_db_work(self, env, caplog):
        """An operator who switched the hold off gets a fans-only cooldown, and the
        refusal costs nothing: it is decided before a session is opened."""
        env.hold_enabled = False
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z, prep.max_z) == ("skipped:disabled", None, None)
        assert env.sessions == 0
        assert env.client.hold_gcode == []
        assert env.client.fans == [(2, 100), (3, 100)]  # the fans are the other actuators, untouched
        assert "plate hold switched off" in caplog.text

    async def test_the_switch_off_is_info_not_a_warning(self, env, caplog):
        """A deliberate operator state must not sit in the channel operators triage."""
        env.hold_enabled = False
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await env.begin()
        levels = {r.levelno for r in caplog.records if "plate hold switched off" in r.getMessage()}
        assert levels == {logging.INFO}

    async def test_the_foreign_refusal_is_asked_first(self, env):
        """Order matters for the log: a foreign plate has no unit to hold at all, so it
        reports ``skipped:foreign`` whatever the switch says."""
        env.hold_enabled = False
        prep = await env.begin(queue_item_id=None)
        assert prep.hold == "skipped:foreign"

    @pytest.mark.parametrize(
        ("target", "expected_z"),
        [
            pytest.param(0, 50.1, id="flush-with-the-plane"),
            pytest.param(-20, 70.1, id="fan-above-the-part"),
            pytest.param(100, 2.0, id="as-high-as-the-part-allows"),
        ],
    )
    async def test_the_target_reaches_the_emitted_gcode(self, env, target, expected_z):
        """The setting is the ONLY thing that moves the hold height for a given part."""
        env.hold_part_top_mm = target
        prep = await env.begin()
        assert prep.hold == "sent"
        assert prep.hold_z == pytest.approx(expected_z)
        hold_line = next(ln for ln in env.client.hold_gcode[0].splitlines() if "; hold:" in ln)
        assert hold_line.startswith(f"G1 Z{expected_z:g} F900")

    async def test_the_recorded_hold_z_is_the_one_that_was_sent(self, env):
        """Parsed back out of the published text rather than re-derived: the record is
        the eject estimator's seed, and a seed that disagrees with the plate's real
        position is exactly what the single-evaluation change exists to prevent."""
        for target in (-50, 0, 33, 100):
            env.client.gcode.clear()
            env.hold_part_top_mm = target
            prep = await env.begin()
            hold_line = next(ln for ln in env.client.hold_gcode[0].splitlines() if "; hold:" in ln)
            assert hold_line.split()[1] == f"Z{prep.hold_z:g}"

    async def test_the_sent_line_names_the_target_and_the_bound(self, env, caplog):
        """``grep "[cooldown-prep]"`` has to be readable per setting: what was asked for,
        what the part actually got, and which constraint decided."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await env.begin()
        line = next(r.getMessage() for r in caplog.records if "plate hold sent" in r.getMessage())
        assert line == (
            "[cooldown-prep] printer 7: plate hold sent "
            "(max_z=50.10 hold_z=2.00 part_top=48.10 target=100 bound=floor "
            "clear_above=51.00 keepout_y=285.00 bbox_y_max=262.00)"
        )

    async def test_a_target_the_model_cannot_grant_is_reported_as_the_ceiling(self, env, caplog):
        """The registry number is the hard cap; the log says so rather than silently
        delivering something other than what was asked for."""
        env.max_z = 55.0  # the profile's ceiling, under a 51 mm clear zone
        env.hold_part_top_mm = 200
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert prep.hold_z == pytest.approx(4.0)  # 55 - 51, the ceiling
        line = next(r.getMessage() for r in caplog.records if "plate hold sent" in r.getMessage())
        assert "part_top=51.00 target=200 bound=ceiling" in line


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
        assert env.client.hold_gcode == []


class TestHoldAdmission:
    """The hold moves the same machine an eject does, so it asks the eject's own
    admission pair — and asks it as LATE as possible, right before the publish."""

    async def test_disconnected_printer(self, env):
        env.manager.connected = False
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:disconnected", None)
        assert env.client.hold_gcode == []

    @pytest.mark.parametrize("refusal", ["z_unreferenced", "job_active"])
    async def test_ejectable_refusals_hold_nothing(self, env, refusal, caplog):
        """A printer that may not be swept may not be moved: a lost Z datum makes every
        absolute Z a hazard, and a live job owns the machine."""
        env.refusal = refusal
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:not_ejectable", None)
        assert refusal in caplog.text  # the refusal token is named, not swallowed
        assert env.client.hold_gcode == []

    async def test_a_refused_eject_admission_still_cools_the_plate(self, env):
        """The fans do NOT ride the hold's admission pair. A printer that may not be
        swept — a lost Z reference, say — is still a printer whose plate has to cool,
        and a human is already standing over it."""
        env.refusal = "z_unreferenced"
        prep = await env.begin()
        assert prep.hold == "skipped:not_ejectable"
        assert env.client.fans == [(2, 100), (3, 100)]
        assert [lane.start for lane in prep.fans] == ["sent", "sent"]

    async def test_refused_publish(self, env):
        env.client.gcode_ok = False
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:publish", None)
        assert len(env.client.hold_gcode) == 1  # attempted exactly once, never retried

    async def test_no_client_at_all(self, env):
        env.manager.client = None
        prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:publish", None)


class TestBeginNeverRaises:
    async def test_db_failure_is_an_outcome_not_an_exception(self, env, caplog):
        """A dead settings/queue DB must not kill the watch that owns the plate gate —
        and must not cost the cooldown its fans either."""
        env.db_error = RuntimeError("queue DB unavailable")
        with caplog.at_level(logging.ERROR, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert (prep.hold, prep.hold_z) == ("skipped:error", None)
        assert "plate hold failed" in caplog.text
        assert env.client.fans == [(2, 100), (3, 100)]  # the fans are armed regardless
        assert all(lane.published for lane in prep.fans)

    async def test_fan_failure_is_an_outcome_not_an_exception(self, env):
        """A publish that throws leaves both lanes unpublished and the hold intact."""

        def boom(fan, percent):
            raise RuntimeError("MQTT down")

        env.client.set_fan_percent = boom
        prep = await env.begin()
        assert prep.hold == "sent"
        assert [lane.start for lane in prep.fans] == ["skipped:error", "skipped:error"]
        assert not any(lane.published for lane in prep.fans)


# --------------------------------------------------------------------------- #
# The two cooldown fans
# --------------------------------------------------------------------------- #
class TestCooldownFans:
    async def test_runs_both_fans_at_their_boost_percents(self, env):
        prep = await env.begin()
        assert env.client.fans == [(2, 100), (3, 100)]  # fan 2 auxiliary, fan 3 chamber
        assert [lane.start for lane in prep.fans] == ["sent", "sent"]
        assert prep.lane("aux").request.boost_percent == 100
        assert prep.lane("chamber").request.sustain_percent == 50

    @pytest.mark.parametrize("fan", cooldown_prep.COOLDOWN_FANS, ids=lambda f: f.name)
    async def test_a_disabled_lane_commands_nothing(self, env, fan, caplog):
        """The switch is the ONE on/off owner per lane — and switching one off leaves
        the other running, because they are independent hardware."""
        other = next(f for f in cooldown_prep.COOLDOWN_FANS if f is not fan)
        fans = _fans(aux_enabled=fan.name != "aux", chamber_enabled=fan.name != "chamber")
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=fans)
        assert prep.lane(fan.name).start == "skipped:disabled"
        assert prep.lane(other.name).start == "sent"
        assert [index for index, _pct in env.client.fans] == [other.index]
        if fan.airduct_cooling(MODEL):
            # A disabled chamber lane never opens the duct: the prelude belongs to the
            # fan it precedes, not to the cooldown.
            assert env.client.airduct == []
        assert f"{fan.name} fan switched off" in caplog.text

    async def test_an_unsupported_model_skips_the_chamber_lane(self, env, caplog):
        """Firmware swallows ``M106 P3`` on an open-frame machine, and a swallowed
        command looks exactly like a working one — so the farm refuses instead."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(model=MODEL_WITHOUT_CHAMBER_FAN)
        assert prep.lane("chamber").start == "skipped:unsupported"
        assert prep.lane("aux").start == "sent"  # the aux lane is ungated by model
        assert env.client.fans == [(2, 100)]
        assert env.client.airduct == []  # nor is the duct touched
        assert "has no chamber fan" in caplog.text

    async def test_an_unknown_model_still_cools_with_the_aux_fan(self, env):
        """A printer whose model the manager has not cached yet is not a printer
        without an aux fan: only the per-model lane fails closed."""
        prep = await env.begin(model=None)
        assert prep.lane("chamber").start == "skipped:unsupported"
        assert prep.lane("aux").start == "sent"

    async def test_the_chamber_lane_opens_the_duct_before_it_spins_the_fan(self, env):
        """The vendor's own pair, in the vendor's own order, on the SAME queue — a flap
        that opens after the fan spins is an exhaust blowing into a closed box."""
        await env.begin()
        assert env.client.airduct == [AIRDUCT_COOLING_GCODE]
        assert env.client.calls.index("gcode:M145 P0") < env.client.calls.index("fan:3")

    async def test_a_fixed_duct_model_runs_the_chamber_fan_with_no_prelude(self, env):
        """ "Has a chamber fan" and "pairs it with a duct prelude" are DIFFERENT per-model
        questions, and the second is narrower: an enclosed P1S has the fan and no flap to
        open, so sending ``M145 P0`` there would be a command firmware swallows."""
        prep = await env.begin(model=MODEL_WITHOUT_AIRDUCT)
        assert prep.lane("chamber").start == "sent"
        assert env.client.fans == [(2, 100), (3, 100)]
        assert env.client.airduct == []
        assert env.client.calls == ["hold", "fan:2", "fan:3"]  # no prelude in between

    async def test_a_fixed_duct_model_cannot_fail_on_an_airduct_it_never_sends(self, env):
        """The refusal only exists where the command does: a P1S never reaches the
        ``skipped:airduct`` arm, however the client would have answered."""
        env.client.airduct_ok = False
        prep = await env.begin(model=MODEL_WITHOUT_AIRDUCT)
        assert prep.lane("chamber").start == "sent"
        assert env.client.fans == [(2, 100), (3, 100)]

    async def test_a_refused_airduct_never_spins_the_chamber_fan(self, env, caplog):
        """The pair is the actuator's two-command contract: a pair that half-lands
        reports not-landed, and the aux lane is untouched by the chamber's failure."""
        env.client.airduct_ok = False
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep = await env.begin()
        assert prep.lane("chamber").start == "skipped:airduct"
        assert env.client.fans == [(2, 100)]  # no (3, …) at all
        assert prep.lane("aux").start == "sent"
        assert "airduct-to-cooling publish refused" in caplog.text

    async def test_a_refused_chamber_publish_leaves_the_aux_lane_intact(self, env):
        env.client.refuse_fans = {3}
        prep = await env.begin()
        assert prep.lane("chamber").start == "skipped:publish"
        assert prep.lane("aux").start == "sent"
        assert env.client.fans == [(2, 100), (3, 100)]  # attempted, and refused

    async def test_a_refused_aux_publish_leaves_the_chamber_lane_intact(self, env):
        env.client.refuse_fans = {2}
        prep = await env.begin()
        assert prep.lane("aux").start == "skipped:publish"
        assert prep.lane("chamber").start == "sent"

    @pytest.mark.parametrize("state", ["RUNNING", "PREPARE", "PAUSE", "SLICING"])
    async def test_never_commands_either_fan_while_a_job_is_active(self, env, state):
        env.manager.state = state
        prep = await env.begin()
        assert env.client.fans == []
        assert env.client.airduct == []  # the duct is not moved under a live job either
        assert [lane.start for lane in prep.fans] == ["skipped:active", "skipped:active"]

    async def test_unreadable_state_is_not_active(self, env):
        """A printer whose state cannot be read is not a printer running a job —
        an idle machine is safe for every actuator."""
        env.manager.state = None
        prep = await env.begin()
        assert env.client.fans == [(2, 100), (3, 100)]
        assert all(lane.published for lane in prep.fans)

    async def test_refused_fan_publish(self, env):
        env.client.fan_ok = False
        prep = await env.begin()
        assert not any(lane.published for lane in prep.fans)
        assert env.sleeps == []  # no witness wait when nothing landed

    async def test_missing_client(self, env):
        env.manager.client = None
        prep = await env.begin()
        assert [lane.start for lane in prep.fans] == ["skipped:no_client", "skipped:no_client"]

    async def test_each_lane_is_witnessed_by_its_own_wire_field(self, env, caplog):
        """``big_fan1_speed`` is the aux duct and ``big_fan2_speed`` the chamber
        exhaust — one witness per lane, never one number standing for both."""
        env.manager.fan = 100
        env.manager.chamber_fan = 47
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=False)
        assert "fan_observed_at_start aux=100 chamber=47" in caplog.text
        summary = _summary(caplog)
        assert "aux=100% start=sent off=skipped:not_wanted observed=100" in summary
        assert "chamber=100%→50% start=sent step=none off=skipped:not_wanted observed=47" in summary

    async def test_witness_is_read_after_the_settle_wait(self, env, caplog):
        """The arm-time witness must be sampled AFTER the wire has had time to report
        the change — a read taken before the settle would witness the old value."""

        async def sleep(seconds):
            env.sleeps.append(seconds)
            env.manager.fan = 100  # the push lands during the wait
            env.manager.chamber_fan = 93

        env.sleep = sleep
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await env.begin(settle_s=1.5)
        assert env.sleeps == [1.5]
        assert "fan_observed_at_start aux=100 chamber=93" in caplog.text

    async def test_begin_returns_before_the_settle_wait(self, env, caplog):
        """No await may sit between the fans ON and the handle: a watch cancelled
        during the settle must already hold the prep so its ``finally`` can retire
        it. The witness therefore waits in ``observe_start``, not in ``begin``."""
        prep = await cooldown_prep.begin(
            PRINTER_ID,
            queue_item_id=ITEM_ID,
            fans=FANS_ON,
            release_threshold_c=THRESHOLD_C,
            model=MODEL,
            hold_enabled=True,
            hold_part_top_mm=HOLD_PART_TOP_DEFAULT,
            settle_s=1.5,
            sleep=env.sleep,
        )
        assert env.client.fans == [(2, 100), (3, 100)]
        assert all(lane.published for lane in prep.fans)
        assert env.sleeps == []  # begin itself never waited
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await prep.observe_start()
        assert env.sleeps == [1.5]
        assert "fan_observed_at_start" in caplog.text

    async def test_observe_start_is_a_no_op_when_nothing_was_published(self, env, caplog):
        prep = await cooldown_prep.begin(
            PRINTER_ID,
            queue_item_id=ITEM_ID,
            fans=_fans(aux_enabled=False, chamber_enabled=False),
            release_threshold_c=THRESHOLD_C,
            model=MODEL,
            hold_enabled=True,
            hold_part_top_mm=HOLD_PART_TOP_DEFAULT,
            settle_s=1.5,
            sleep=env.sleep,
        )
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            await prep.observe_start()
        assert env.sleeps == []
        assert "fan_observed_at_start" not in caplog.text

    async def test_a_quantised_witness_is_logged_not_enforced(self, env, caplog):
        """The wire reports a 0-15 LEVEL rescaled to percent, so an observation
        legitimately differs from the commanded percent. It is evidence, never a
        confirmation: a mismatch changes NOTHING about the prep."""
        env.manager.fan = 67  # what a 70 % command comes back as
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=_fans(aux_percent=70))
            prep.end(fan_off=False)
        assert prep.lane("aux").published is True
        assert "fan_observed_at_start aux=67" in caplog.text
        assert "aux=70% start=sent off=skipped:not_wanted observed=67" in _summary(caplog)

    async def test_switching_the_fans_off_never_touches_the_airduct(self, env):
        """The farm owns the duct mode for the whole cooldown and leaves it in cooling
        — the vendor's own finish tail does the same, and the next start block sets its
        own mode anyway."""
        prep = await env.begin()
        env.client.gcode.clear()
        prep.end(fan_off=True)
        assert env.client.airduct == []
        assert env.client.gcode == []


# --------------------------------------------------------------------------- #
# The chamber boost, and the ONE sample that ends it
# --------------------------------------------------------------------------- #
class TestBoost:
    """The chamber lane boosts while the chamber air is still above the eject line and
    steps to its sustain speed the first sample it is not. Measured live per cooldown:
    there is no constant, no stored minute count and no second timer anywhere here."""

    async def test_the_first_sample_records_the_arm_readings(self, env):
        """Diagnostic, and the thing that makes an ``after 0 s`` boost readable as a
        late re-arm rather than a broken comparison."""
        prep = await env.begin()
        prep.note_sample({"bed": 61.0, "chamber": 38.0})
        assert prep.chamber_at_arm_c == 38.0
        assert prep.bed_at_arm_c == 61.0
        prep.note_sample({"bed": 46.0, "chamber": 35.0})
        assert (prep.chamber_at_arm_c, prep.bed_at_arm_c) == (38.0, 61.0)  # the ARM reading, not the latest

    async def test_no_step_while_the_chamber_is_above_the_threshold(self, env):
        """The measured curve: the chamber air sat at 38 → 35 → 33 over 16.5 minutes,
        and the boost is exactly the part of the wait where it is still above the line."""
        prep = await env.begin()
        env.client.fans.clear()
        for chamber in (38.0, 35.0, 33.1):
            prep.note_sample({"bed": 50.0, "chamber": chamber})
        assert env.client.fans == []
        assert prep.boost_ended_at is None
        assert prep.lane("chamber").step is None

    async def test_steps_on_the_first_sample_at_or_under_the_threshold(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.client.fans.clear()
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
        assert env.client.fans == [(3, 50)]  # the chamber lane alone steps
        assert prep.lane("chamber").step == "sent"
        assert prep.lane("aux").step == "skipped:same"  # no step-down exists for that lane
        assert prep.boost_ended_at is not None
        assert len(_boost_lines(caplog)) == 1
        assert "chamber=33.0 chamber_at_arm=33.0 bed=40.0 threshold=33.0" in _boost_lines(caplog)[0]

    async def test_the_step_fires_exactly_once(self, env, caplog):
        """A stamp, not a comparison repeated every 20 s: the fan is already at its
        sustain speed, and re-publishing it would be noise for the rest of the wait."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
            env.client.fans.clear()
            prep.note_sample({"bed": 36.0, "chamber": 31.0})
            prep.note_sample({"bed": 34.0, "chamber": 30.0})
        assert env.client.fans == []
        assert len(_boost_lines(caplog)) == 1

    async def test_a_chamber_already_under_the_threshold_at_arm_steps_immediately(self, env, caplog):
        """A restart re-arming late in a cooldown, or a shop cooler than the threshold:
        the exhaust's unique work is already done, and the log says ``after 0 s``
        rather than pretending a boost window happened."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.note_sample({"bed": 45.0, "chamber": 30.0})
        assert prep.lane("chamber").step == "sent"
        assert "chamber boost ended after 0 s" in _boost_lines(caplog)[0]

    async def test_a_chamber_that_never_reaches_the_threshold_never_steps(self, env, caplog):
        """Fail-open toward MORE cooling: the boost simply runs the whole wait."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            for chamber in (38.0, 36.0, 34.0):
                prep.note_sample({"bed": 50.0, "chamber": chamber})
            prep.end(fan_off=True)
        assert _boost_lines(caplog) == []
        summary = _summary(caplog)
        assert "chamber_boost_ended_after=never" in summary
        assert "chamber=100%→50% start=sent step=none" in summary

    async def test_a_missing_chamber_reading_never_steps(self, env):
        """A model with no chamber sensor reports no ``chamber`` key at all — the same
        case as one that never gets there, and never a comparison against None."""
        prep = await env.begin()
        env.client.fans.clear()
        prep.note_sample({"bed": 40.0})
        assert prep.boost_ended_at is None
        assert prep.chamber_at_arm_c is None
        assert prep.bed_at_arm_c == 40.0
        assert env.client.fans == []

    async def test_a_sustain_of_zero_stops_the_fan_at_the_boost_end(self, env, caplog):
        """0 is a step TARGET, not a second switch: the fan stops when the chamber
        reaches the line, and ``end`` then has nothing left to switch off."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=_fans(chamber_sustain=0))
            prep.note_sample({"bed": 40.0, "chamber": 32.0})
            assert env.client.fans == [(2, 100), (3, 100), (3, 0)]
            prep.end(fan_off=True)
        assert prep.lane("chamber").step == "sent"
        assert prep.lane("chamber").off == "skipped:already_off"
        assert env.client.fans == [(2, 100), (3, 100), (3, 0), (2, 0)]  # only the aux lane is switched off
        assert "chamber=100%→0% start=sent step=sent off=skipped:already_off" in _summary(caplog)

    async def test_an_unpublished_lane_records_not_published(self, env):
        """The boost still ENDS — it is a fact about the chamber air, not about a fan —
        but a lane that never started has nothing to step."""
        prep = await env.begin(fans=_fans(chamber_enabled=False))
        env.client.fans.clear()
        prep.note_sample({"bed": 40.0, "chamber": 33.0})
        assert prep.boost_ended_at is not None
        assert prep.lane("chamber").step == "skipped:not_published"
        assert prep.lane("aux").step == "skipped:same"
        assert env.client.fans == []

    async def test_a_step_is_refused_while_a_job_is_active(self, env):
        """The real sequence: the bed hit the release line first, the eject sweep is
        already running, and only then does the chamber air reach the threshold. That
        job owns the machine and carries its own fan lines — the step stands aside."""
        prep = await env.begin()
        env.manager.state = "RUNNING"
        env.client.fans.clear()
        prep.note_sample({"bed": 40.0, "chamber": 33.0})
        assert prep.lane("chamber").step == "skipped:active"
        assert env.client.fans == []

    async def test_a_step_with_no_client(self, env):
        prep = await env.begin()
        env.manager.client = None
        prep.note_sample({"bed": 40.0, "chamber": 33.0})
        assert prep.lane("chamber").step == "skipped:no_client"

    async def test_a_refused_step_publish(self, env, caplog):
        prep = await env.begin()
        env.client.refuse_fans = {3}
        with caplog.at_level(logging.WARNING, logger=cooldown_prep.__name__):
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
        assert prep.lane("chamber").step == "skipped:publish"
        assert "chamber fan step publish refused" in caplog.text

    async def test_note_sample_never_raises(self, env, caplog):
        """It runs inside the bed poll that owns the plate's watch: a measurement is
        never worth a stranded gate."""
        prep = await env.begin()

        def boom(printer_id):
            raise RuntimeError("manager down")

        env.manager.get_status = boom
        with caplog.at_level(logging.ERROR, logger=cooldown_prep.__name__):
            prep.note_sample({"bed": 40.0, "chamber": 33.0})  # must not propagate
        assert prep.lane("aux").step == "skipped:same"  # decided before any wire read
        assert prep.lane("chamber").step is None
        assert "cooldown sample failed" in caplog.text


# --------------------------------------------------------------------------- #
# end(): the fan hand-over and the ONE summary line
# --------------------------------------------------------------------------- #
class TestEnd:
    async def test_switches_both_fans_off_when_nothing_wants_them(self, env, caplog):
        """The whole publish history of a cooldown that boosted, stepped and ended."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
            prep.end(fan_off=True)
        assert env.client.fans == [(2, 100), (3, 100), (3, 50), (2, 0), (3, 0)]
        assert [lane.off for lane in prep.fans] == ["sent", "sent"]
        assert "off=sent" in _summary(caplog)

    async def test_hands_both_fans_over_when_a_successor_wants_them(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=False)
        assert env.client.fans == [(2, 100), (3, 100)]  # left running for the next watch
        assert [lane.off for lane in prep.fans] == ["skipped:not_wanted", "skipped:not_wanted"]

    @pytest.mark.parametrize("fan", cooldown_prep.COOLDOWN_FANS, ids=lambda f: f.name)
    async def test_never_switches_off_a_fan_it_did_not_start(self, env, fan, caplog):
        """Turning off a fan this module never turned on would be commanding hardware
        it does not own — an operator's manual ``/fan-speed``, say."""
        fans = _fans(aux_enabled=fan.name != "aux", chamber_enabled=fan.name != "chamber")
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=fans)
            prep.end(fan_off=True)
        assert prep.lane(fan.name).off == "skipped:not_published"
        assert (fan.index, 0) not in env.client.fans

    async def test_never_switches_off_during_the_eject_it_released_into(self, env, caplog):
        """The normal release exit: the sweep is already running and carries its own
        ``M106 P2 S0`` / ``M106 P3 S0``. Commanding one here would race that job."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.manager.state = "RUNNING"
            prep.end(fan_off=True)
        assert env.client.fans == [(2, 100), (3, 100)]
        assert [lane.off for lane in prep.fans] == ["skipped:active", "skipped:active"]

    async def test_missing_client_at_exit(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.manager.client = None
            prep.end(fan_off=True)
        assert [lane.off for lane in prep.fans] == ["skipped:no_client", "skipped:no_client"]

    async def test_refused_off_publish(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.client.fan_ok = False
            prep.end(fan_off=True)
        assert [lane.off for lane in prep.fans] == ["skipped:publish", "skipped:publish"]

    async def test_one_lanes_off_exception_does_not_skip_the_others(self, env, caplog):
        """Two independent fans: a throw on the first must not leave the second
        running under an idle printer, which nothing else in the farm clears."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()

            def boom(fan, percent):
                if fan == 2:
                    raise RuntimeError("MQTT down")
                env.client.fans.append((fan, percent))
                return True

            env.client.set_fan_percent = boom
            prep.end(fan_off=True)
        assert prep.lane("aux").off == "skipped:publish"
        assert prep.lane("chamber").off == "sent"
        assert (3, 0) in env.client.fans

    async def test_end_never_raises(self, env, caplog):
        def boom(fan, percent):
            raise RuntimeError("MQTT down")

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            env.client.set_fan_percent = boom
            prep.end(fan_off=True)  # must not propagate — it runs in the watch's finally
        assert [lane.off for lane in prep.fans] == ["skipped:publish", "skipped:publish"]
        assert "cooldown ended after" in caplog.text

    async def test_summary_line_shape_when_the_plate_was_held(self, env, caplog):
        """The ONE greppable line the wave is measured by, pinned whole. Every other
        summary assertion in this file reads a FIELD of it — one whole-string pin is
        enough to own the shape, and more would make the format unchangeable."""
        env.manager.fan = 100
        env.manager.chamber_fan = 93
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
        assert _summary(caplog) == (
            "[cooldown-prep] printer 7: cooldown ended after 0 s "
            "(hold=sent max_z=50.10 hold_z=2.00, "
            "chamber_boost_ended_after=never chamber_at_arm=none bed_at_arm=none, "
            "aux=100% start=sent off=sent observed=100, "
            "chamber=100%→50% start=sent step=none off=sent observed=93)"
        )

    async def test_the_summary_names_the_boost_window_it_measured(self, env, caplog):
        """THE number this wave exists to take: how long the exhaust needed to bring the
        chamber air to the eject line. Unmeasured before production, so the line is the
        instrument."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.note_sample({"bed": 61.0, "chamber": 38.0})
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
            prep.end(fan_off=True)
        summary = _summary(caplog)
        assert "chamber_boost_ended_after=0 s" in summary
        assert "chamber_at_arm=38.0 bed_at_arm=61.0" in summary
        assert "chamber=100%→50% start=sent step=sent off=sent" in summary

    async def test_a_stepping_lane_keeps_its_shape_when_the_two_speeds_are_equal(self, env, caplog):
        """The grep shape per lane is a property of the FAN, not of the numbers. An
        operator who sets the chamber sustain to its boost still gets the arrow form and
        a ``step=`` field — otherwise the measurement would change shape exactly when
        someone had been experimenting with the setting."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=_fans(chamber_sustain=100))
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
            prep.end(fan_off=True)
        # The numeric no-op is still reported honestly as a step that did nothing …
        assert prep.lane("chamber").step == "skipped:same"
        assert env.client.fans == [(2, 100), (3, 100), (2, 0), (3, 0)]  # no step published
        # … and the segment keeps the stepping shape.
        assert "chamber=100%→100% start=sent step=skipped:same off=sent" in _summary(caplog)

    async def test_the_aux_lane_never_renders_a_step_field(self, env, caplog):
        """It has no sustain setting to step to, so a ``step=`` there would be a field
        no operator can act on."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.note_sample({"bed": 40.0, "chamber": 33.0})
            prep.end(fan_off=True)
        summary = _summary(caplog)
        aux_segment = next(part for part in summary.split(", ") if part.startswith("aux="))
        assert "step=" not in aux_segment
        assert aux_segment == "aux=100% start=sent off=sent observed=none"

    async def test_summary_fields_when_the_hold_was_skipped(self, env, caplog):
        """Same shape on the fans-only path — one line whatever happened, so the
        per-printer medians are comparable across H2S and H2C."""
        env.geometry = FAN_ONLY_GEOMETRY
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
        summary = _summary(caplog)
        assert "hold=skipped:no_numbers max_z=none hold_z=none" in summary
        assert "aux=100% start=sent off=sent observed=none" in summary

    async def test_summary_fields_with_a_lane_switched_off(self, env, caplog):
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(fans=_fans(chamber_enabled=False))
            prep.end(fan_off=True)
        summary = _summary(caplog)
        assert "chamber=100%→50% start=skipped:disabled" in summary
        assert "off=skipped:not_published" in summary
        assert "aux=100% start=sent off=sent" in summary


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
            pytest.param(lambda e: setattr(e, "hold_enabled", False), id="disabled"),
        ],
    )
    async def test_skip_leaves_no_seed(self, env, mutate):
        mutate(env)
        prep = await env.begin()
        assert prep.hold != "sent"
        assert prep.hold_z is None


# --------------------------------------------------------------------------- #
# Maintenance mode: the fans run, the plate is not moved
# --------------------------------------------------------------------------- #
class TestServiceHold:
    """A held printer's plate cools on the FANS alone.

    The hold's line is motion, not air: raising the plate drives the bed and the
    toolhead's ``G150.3`` on a machine whose operator may have their hands inside it,
    while switching a fan on changes nothing they can be hurt by. The cost is accepted
    rather than hidden — an unheld plate is held at the nozzle plane where the aux stream
    actually is, ~73 mm above a vendor-parked part, so a cooldown that arms under a hold
    is slower — and it is NOT re-attempted when the hold lifts.
    """

    async def test_the_hold_is_refused_and_no_session_is_opened(self, env, caplog):
        """Asked before any state is read: no queue item, no profile, no geometry row and
        no donor 3MF can change the answer, so none of them is fetched."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin(held=True)

        assert prep.hold == "skipped:service_hold"
        assert prep.hold_z is None
        assert env.sessions == 0  # not one DB session opened for a decision already made
        assert env.client.hold_gcode == []  # and nothing was published at the machine
        assert "maintenance mode — fans only, plate not held" in caplog.text

    async def test_both_fans_still_run_under_a_hold(self, env):
        """THE operator requirement: under maintenance mode the cooldown runs as in
        production — aux fan on, chamber boosting — with no eject."""
        prep = await env.begin(held=True)

        assert env.client.fans == [(2, 100), (3, 100)]
        assert [lane.start for lane in prep.fans] == ["sent", "sent"]

    async def test_the_chamber_lane_still_steps_down_under_a_hold(self, env):
        """The boost's end is a fact about the chamber AIR, not about who owns the
        printer: the exhaust's unique work finishes at the eject line either way."""
        prep = await env.begin(held=True)
        env.client.fans.clear()

        prep.note_sample({"bed": 40.0, "chamber": 33.0})

        assert prep.lane("chamber").step == "sent"
        assert env.client.fans == [(3, 50)]

    async def test_the_hold_is_asked_after_the_operators_own_switch(self, env, caplog):
        """Both are static refusals; the operator's switch is reported first so a farm
        that has the hold switched off altogether reads the same way held or not."""
        env.hold_enabled = False
        prep = await env.begin(held=True)
        assert prep.hold == "skipped:disabled"

    async def test_a_foreign_plate_is_still_reported_as_foreign(self, env):
        """The foreign refusal comes first — it is the deepest fact (no unit, no donor,
        no measured part height), and a held foreign plate is not a different case."""
        prep = await env.begin(queue_item_id=None, held=True)
        assert prep.hold == "skipped:foreign"


# --------------------------------------------------------------------------- #
# RETIRED: no actuator after the cooling episode ends
# --------------------------------------------------------------------------- #
class TestRetiredInvariant:
    """The prep's lifetime is the COOLING EPISODE, and the episode can now end before the
    watch does: a service hold withholds the eject, so the watch retires the actuators at
    the cooldown's end and keeps polling. Everything that drives a fan therefore refuses
    on a retired prep — otherwise the sampler restarts the chamber fan at its sustain
    speed on the first chamber-under-threshold sample AFTER the retirement, which on the
    measured trace is exactly what happens (the bed reaches the eject line first).
    """

    async def test_a_sample_after_end_steps_nothing(self, env, caplog):
        """The H1 shape, in order: the bed crosses the line, the fans are retired, and
        the chamber only then reaches the threshold."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
            assert env.client.fans == [(2, 100), (3, 100), (2, 0), (3, 0)]
            env.client.fans.clear()

            prep.note_sample({"bed": 30.0, "chamber": 32.0})

        assert env.client.fans == []  # no fan was brought back
        assert prep.boost_ended_at is None  # and no measurement was taken after the end
        assert prep.lane("chamber").step is None
        assert _boost_lines(caplog) == []

    async def test_a_step_on_a_retired_prep_says_so(self, env):
        """The invariant is per-METHOD, not only per-caller: the sampler's early return is
        what production relies on, and this is the outcome any later caller would get."""
        prep = await env.begin()
        prep.end(fan_off=True)

        assert prep.retired is True
        assert prep._step_fan(prep.lane("chamber")) == "skipped:retired"

    async def test_end_twice_logs_one_summary(self, env, caplog):
        """The MEASUREMENT is idempotent: one ``cooldown ended after N s`` line per
        cooling episode, from the first call. ``_summary`` asserts exactly one."""
        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep = await env.begin()
            prep.end(fan_off=True)
            prep.end(fan_off=True)

        assert "off=sent" in _summary(caplog)

    async def test_a_lane_whose_first_off_was_skipped_active_is_retired_on_the_second_end(self, env, caplog):
        """The H2 shape. A deferral has no eject job whose prologue carries the durable
        ``M106 P2 S0``, so a first OFF that landed on ``skipped:active`` (a foreign job was
        running) would leave both fans on for the length of the hold. The exit ``finally``
        IS the retry, and it logs when one lands."""
        prep = await env.begin()
        env.manager.state = "RUNNING"  # a job owns the machine at the deferral edge
        prep.end(fan_off=True)
        assert [lane.off for lane in prep.fans] == ["skipped:active", "skipped:active"]
        env.client.fans.clear()
        env.manager.state = "FINISH"  # …and it has ended by the time the watch exits

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep.end(fan_off=True)

        assert env.client.fans == [(2, 0), (3, 0)]
        assert [lane.off for lane in prep.fans] == ["sent", "sent"]
        assert caplog.text.count("landed on the retirement retry") == 2
        assert "first attempt skipped:active" in caplog.text

    async def test_a_landed_off_is_never_re_published(self, env):
        """``sent`` and ``skipped:already_off`` are the two outcomes that mean the fan is
        actually stopped; nothing re-commands those."""
        prep = await env.begin(fans=_fans(chamber_sustain=0))
        prep.note_sample({"bed": 40.0, "chamber": 32.0})  # steps the chamber lane to 0
        prep.end(fan_off=True)
        assert [lane.off for lane in prep.fans] == ["sent", "skipped:already_off"]
        env.client.fans.clear()

        prep.end(fan_off=True)

        assert env.client.fans == []

    async def test_the_retry_is_skipped_when_the_fans_were_handed_over(self, env):
        """``fan_off=False`` means a successor cooldown-class watch still wants them —
        the same question on the second call as on the first."""
        prep = await env.begin()
        prep.end(fan_off=False)
        env.client.fans.clear()

        prep.end(fan_off=False)

        assert env.client.fans == []
        assert [lane.off for lane in prep.fans] == ["skipped:not_wanted", "skipped:not_wanted"]


# --------------------------------------------------------------------------- #
# The operator's own hand on a fan
# --------------------------------------------------------------------------- #
class TestOperatorFanOff:
    """A fan an operator switches off by hand (touchscreen, or ``POST /fan-speed``) is
    THEIRS, and the farm does not re-assert it.

    The wire is the only origin that also sees the touchscreen, so the detector lives on
    the poll that was already running. Zero is the one unambiguous witness value — the
    wire reports a quantised 0-15 LEVEL, so a fan that was merely turned DOWN is not
    detected, and that limit is stated rather than papered over. It fails OPEN: a lane
    never witnessed running never arms the detector at all.
    """

    async def test_a_witnessed_lane_switched_off_by_hand_is_released_to_the_operator(self, env, caplog):
        prep = await env.begin()
        env.manager.chamber_fan = 15  # the wire says the exhaust is running
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        assert prep.lane("chamber").witnessed_on is True
        env.client.fans.clear()
        env.manager.chamber_fan = 0  # …and now it is not, and we did not do that

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep.note_sample({"bed": 40.0, "chamber": 33.0})  # the boost would step here

        assert prep.lane("chamber").operator_off is True
        assert prep.lane("chamber").step == "skipped:operator_off"
        assert env.client.fans == []  # the step is a RE-ASSERTION, and it stood aside
        assert "chamber fan observed off — released to the operator, not re-commanded" in caplog.text

    async def test_it_is_said_once_per_lane(self, env, caplog):
        prep = await env.begin()
        env.manager.chamber_fan = 15
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        env.manager.chamber_fan = 0

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            for _ in range(3):
                prep.note_sample({"bed": 45.0, "chamber": 36.0})

        assert caplog.text.count("released to the operator") == 1

    async def test_the_end_of_cooldown_off_is_not_suppressed(self, env, caplog):
        """The OFF retires a fan the FARM published: an operator who switched it back on
        gets it switched off with the cooldown, rather than left running on an idle
        printer — which is a state nothing else in the farm clears."""
        prep = await env.begin()
        env.manager.fan = 15
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        env.manager.fan = 0
        prep.note_sample({"bed": 45.0, "chamber": 36.0})
        assert prep.lane("aux").operator_off is True
        env.client.fans.clear()

        with caplog.at_level(logging.INFO, logger=cooldown_prep.__name__):
            prep.end(fan_off=True)

        assert env.client.fans == [(2, 0), (3, 0)]
        assert prep.lane("aux").off == "sent"

    async def test_our_own_sustain_zero_is_not_read_as_the_operator(self, env):
        """``sustain_percent == 0`` is a step TARGET: that zero on the wire is ours, and
        misreading it would make every zero-sustain cooldown claim a human intervened."""
        prep = await env.begin(fans=_fans(chamber_sustain=0))
        env.manager.chamber_fan = 15
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        prep.note_sample({"bed": 40.0, "chamber": 33.0})  # our step publishes S0
        assert prep.lane("chamber").step == "sent"
        env.manager.chamber_fan = 0  # …and the wire duly reports it

        prep.note_sample({"bed": 36.0, "chamber": 31.0})

        assert prep.lane("chamber").operator_off is False

    async def test_a_lane_the_wire_never_reported_running_still_steps(self, env):
        """The virtual printer's shape (and a model that does not report the field):
        ``big_fan2_speed`` reads 0 from the start, which is no evidence of anything —
        arming the detector on it would suppress every step on that machine."""
        env.manager.chamber_fan = 0
        prep = await env.begin()
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        env.client.fans.clear()

        prep.note_sample({"bed": 40.0, "chamber": 33.0})

        assert prep.lane("chamber").witnessed_on is False
        assert prep.lane("chamber").operator_off is False
        assert prep.lane("chamber").step == "sent"
        assert env.client.fans == [(3, 50)]

    async def test_a_none_witness_decides_nothing(self, env):
        """An unreadable witness is not a zero. A printer that drops off the wire
        mid-cooldown must not be read as an operator switching its fans off."""
        prep = await env.begin()
        env.manager.chamber_fan = 15
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        env.manager.chamber_fan = None

        prep.note_sample({"bed": 45.0, "chamber": 36.0})

        assert prep.lane("chamber").witnessed_on is True
        assert prep.lane("chamber").operator_off is False

    async def test_an_unpublished_lane_is_never_watched(self, env):
        """A fan the farm never commanded ON was never ours to decide about — an operator
        running the aux fan themselves is not something to detect or to stop."""
        prep = await env.begin(fans=_fans(chamber_enabled=False))
        env.manager.chamber_fan = 15
        prep.note_sample({"bed": 50.0, "chamber": 38.0})
        env.manager.chamber_fan = 0

        prep.note_sample({"bed": 45.0, "chamber": 36.0})

        assert prep.lane("chamber").witnessed_on is False
        assert prep.lane("chamber").operator_off is False

    async def test_a_witness_read_that_throws_never_costs_the_step_its_decision(self, env, caplog):
        """The detector is a diagnostic riding the sampler; the step below it is the
        actuator. A manager that throws must not take the second with the first."""
        prep = await env.begin()

        def boom(printer_id):
            raise RuntimeError("manager down")

        env.manager.get_status = boom
        with caplog.at_level(logging.ERROR, logger=cooldown_prep.__name__):
            prep.note_sample({"bed": 40.0, "chamber": 33.0})

        assert "fan witness read failed" in caplog.text
        assert prep.lane("aux").step == "skipped:same"  # decided before any wire read


async def test_begin_runs_the_hold_before_the_fans(env):
    """Order matters: the hold wants the machine in exactly the idle state the end
    block left it, and nothing should sit between the terminal and that motion. Then
    the aux fan, then the duct-and-chamber pair in the vendor's own order."""
    await env.begin()
    assert env.client.calls == ["hold", "fan:2", "gcode:M145 P0", "fan:3"]


async def test_settle_wait_defaults_to_the_event_loops_own_sleep(env):
    """``sleep``/``settle_s`` are injected for tests only — production waits on the
    real loop, which is why every test here passes its own."""
    import inspect

    signature = inspect.signature(cooldown_prep.begin)
    assert signature.parameters["sleep"].default is asyncio.sleep
    assert signature.parameters["settle_s"].default == 3.0
