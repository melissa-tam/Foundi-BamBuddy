"""``spool_selection.backup_partner_gap`` + its dispatch-time wiring in the scheduler.

010-H2S ran out on slot 2 twice in 28 h with black PETG loaded in slot 4 and AMS
Filament Backup ON. The firmware never auto-switched because slot 2's tray colour was
``161616FF`` and slot 4's ``000000FF`` — the firmware pairs backup slots only on an
EXACT preset + colour match, while the farm's matcher (``colors_are_similar``, ≤40 per
channel) calls them the same filament and dispatched onto slot 2 believing slot 4 backed
it up (incident shape 33).

Since the firmware's key has exactly two dimensions and a near miss has already agreed
on the preset, a gap can only ever be a COLOUR gap. Nozzle temperature was carried as a
third dimension between 2026-08-21 and 2026-08-25 and is not one — the firmware's own
``filam_bak`` masks put a slot at 230-260 in one group with slots at 230-270.

The reconcile lane harmonises the farm's OWN tagless slots onto the canonical identity.
These tests pin the VISIBILITY lane for the residue it cannot fix — an RFID or
operator-bound tray, a refused AMS write, a dispatch before the first idle window:

* the pure rule (no scheduler, no wire, no DB), and
* the scheduler wiring: one WARN in the agreed format per dispatch that sees the gap,
  one page per (printer, group-pair) per 6 h, silence when there is no gap, and a
  dispatch that survives an exception inside the whole block.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import notify_dedup
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.spool_selection import backup_partner_gap

# The 010-H2S constellation, as the wire reported it.
_BLACK = "000000FF"  # what every RFID PETG-HF roll reports
_NEAR_BLACK = "161616FF"  # the PETG-HF preset default a touchscreen edit accepted


def _tray(
    tray_id: int = 0,
    *,
    color: str | None = _BLACK,
    info_idx: str | None = "GFG02",
    tray_type: str | None = "PETG",
    tmin: object = 230,
    tmax: object = 270,
    state: int = 10,
    global_tray_id: int | None = None,
) -> dict:
    """One live tray dict in the shape ``raw_data["ams"][n]["tray"][m]`` carries.

    ``global_tray_id`` is the stamp the scheduler adds before handing peers to the pure
    function; it defaults to the single-unit arithmetic so a test can name slots by
    their tray id alone.
    """
    return {
        "id": tray_id,
        "state": state,
        "tray_type": tray_type,
        "tray_info_idx": info_idx,
        "tray_color": color,
        "nozzle_temp_min": tmin,
        "nozzle_temp_max": tmax,
        "global_tray_id": tray_id if global_tray_id is None else global_tray_id,
    }


class TestBackupPartnerGapRule:
    """The pure rule — the part worth pinning, testable without a scheduler."""

    def test_colour_gap_names_both_values(self):
        """The incident itself: one preset, two colours the firmware will not pair."""
        gap = backup_partner_gap(_tray(1, color=_NEAR_BLACK), [_tray(3, color=_BLACK)])
        assert gap is not None
        assert gap.dimension == "colour"
        assert gap.picked_value == "#161616"
        assert gap.partner_value == "#000000"
        assert gap.partner_global_tray_id == 3
        # The two keys the caller dedups on are the ones the verdict rests on.
        assert gap.picked_key == "tray:GFG02|color:161616"
        assert gap.partner_key == "tray:GFG02|color:000000"

    def test_real_partner_wins_over_a_near_miss(self):
        """A tray sharing the key means the runout self-heals — no warning, even though
        a near-miss also sits on the printer. The exact-partner scan therefore has to
        cover the WHOLE list before any near-miss is reported."""
        picked = _tray(1, color=_NEAR_BLACK)
        others = [_tray(2, color=_BLACK), _tray(3, color=_NEAR_BLACK)]
        assert backup_partner_gap(picked, others) is None

    def test_partner_ordering_does_not_hide_an_exact_match(self):
        """Same as above with the near-miss FIRST — order must not decide the verdict."""
        picked = _tray(1, color=_NEAR_BLACK)
        others = [_tray(3, color=_NEAR_BLACK), _tray(2, color=_BLACK)]
        assert backup_partner_gap(picked, others) is None

    def test_different_preset_is_not_a_near_miss(self):
        """A generic GFG99 beside a GFG02 is a different roll, not a near miss: the
        operator answer there is not 'make the colours match'."""
        picked = _tray(1, color=_NEAR_BLACK)
        assert backup_partner_gap(picked, [_tray(3, color=_BLACK, info_idx="GFG99")]) is None

    def test_a_different_nozzle_temp_range_is_not_a_gap(self):
        """Byte-identical preset and colour, different nozzle-temp range — ONE group, so
        there is a real partner and nothing to page about. The firmware's own
        ``filam_bak`` says so (010-H2S, mask ``[15]``: a tagged slot at 230-260 grouped
        with three tagless slots at 230-270), and a tray reporting no range at all is a
        partner on the same terms. Paging here would send an operator to edit a
        temperature that changes nothing."""
        assert backup_partner_gap(_tray(1), [_tray(3, tmax=260)]) is None
        assert backup_partner_gap(_tray(1, tmin=None, tmax=None), [_tray(3)]) is None

    def test_unread_and_empty_peers_are_ignored(self):
        """A seated-but-unread tray and a wire-asserted-empty one are peers for nothing,
        so neither counts as a near miss NOR as an exact partner.

        The empty peer carries the asserted-cleared shape the raw normaliser injects
        (``bambu_mqtt._normalize_cleared_trays``) — a bare ``state: 9`` beside a live
        ``tray_type`` is presence-UNKNOWN, not empty, and still belongs to its group."""
        unread = {"id": 2, "state": 10, "global_tray_id": 2}
        empty = {"id": 3, "state": 9, "tray_type": "", "tray_info_idx": "", "tray_color": "", "global_tray_id": 3}
        assert backup_partner_gap(_tray(1, color=_NEAR_BLACK), [unread, empty]) is None

    def test_no_others_at_all(self):
        assert backup_partner_gap(_tray(1), []) is None

    def test_picked_tray_with_no_key_of_its_own(self):
        """A tray the firmware cannot group (unread) can neither have nor lack a partner."""
        assert backup_partner_gap({"id": 1, "state": 10}, [_tray(3)]) is None

    def test_colour_far_apart_is_a_different_filament(self):
        """The near-miss test is the MATCHER's reading — a red roll beside a black one is
        simply another filament, and pooling was never expected."""
        assert backup_partner_gap(_tray(1, color="FF0000FF"), [_tray(3, color=_BLACK)]) is None


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------
# ``slot=`` is the REQUIREMENT slot (the mapping position), the same vocabulary the
# matcher's own ``[spool-select] slot=… -> picked gtid=…`` trace uses. The human AMS
# slot name ("AMS A slot 2") belongs to the operator copy, not to the engineer's trace.
_WARN_HEAD = "[spool-select] printer=10 slot=1 picked gtid=1 has NO firmware backup partner"


def _status(trays: list[dict]) -> SimpleNamespace:
    """A live printer state carrying one AMS unit with ``trays`` in it."""
    return SimpleNamespace(
        ams_filament_backup=True,
        ams_extruder_map={},
        raw_data={"ams": [{"id": 0, "tray": trays}]},
    )


def _split_group_trays() -> list[dict]:
    """The 010-H2S constellation: slot 2 (gtid 1) near-black, slot 4 (gtid 3) black."""
    return [_tray(1, color=_NEAR_BLACK), _tray(3, color=_BLACK)]


@pytest.fixture
def scheduler():
    return PrintScheduler()


@pytest.fixture(autouse=True)
def clean_dedup():
    """The dedup ledger is process-lifetime module state — isolate every case."""
    notify_dedup._reset_state()
    yield
    notify_dedup._reset_state()


async def _run(scheduler, status, mapping, *, backup_on=True, dual=False, extruder_map=None):
    """Drive ``_warn_backup_group_gap`` against a stubbed wire + DB, returning the
    notification mock the assertions read. Everything the method touches beyond the
    pure rule is a patch here: the backup context, the live status, the printer row
    and the notification itself."""
    notify = AsyncMock()
    with (
        patch(
            "backend.app.services.print_scheduler._get_printer_backup_context",
            new=AsyncMock(return_value=(backup_on, extruder_map or {}, dual)),
        ),
        patch("backend.app.services.print_scheduler.printer_manager.get_status", return_value=status),
        patch.object(scheduler, "_get_printer", new=AsyncMock(return_value=SimpleNamespace(name="010-H2S"))),
        patch(
            "backend.app.services.print_scheduler.notification_service.on_backup_group_split",
            new=notify,
        ),
    ):
        await scheduler._warn_backup_group_gap(MagicMock(), 10, mapping)
    return notify


class TestSchedulerWiring:
    @pytest.mark.asyncio
    async def test_split_group_warns_and_pages(self, scheduler, caplog):
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, _status(_split_group_trays()), [1])

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0] == (f"{_WARN_HEAD} — gtid=3 is the same filament but differs in colour (#161616 vs #000000)")

        notify.assert_awaited_once()
        kwargs = notify.await_args.kwargs
        assert kwargs["printer_id"] == 10
        assert kwargs["printer_name"] == "010-H2S"
        assert kwargs["slot"] == "AMS A slot 2"
        assert kwargs["partner_slot"] == "AMS A slot 4"
        assert kwargs["dimension"] == "colour"
        assert (kwargs["picked_value"], kwargs["partner_value"]) == ("#161616", "#000000")

    @pytest.mark.asyncio
    async def test_second_dispatch_inside_the_window_does_not_page_again(self, scheduler, caplog):
        status = _status(_split_group_trays())
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            first = await _run(scheduler, status, [1])
            second = await _run(scheduler, status, [1])

        first.assert_awaited_once()
        second.assert_not_awaited()
        # The WARN is the triage record and is NOT deduped — both dispatches logged.
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2

    @pytest.mark.asyncio
    async def test_backup_off_says_nothing(self, scheduler, caplog):
        """With AMS Filament Backup OFF there is no group to be split — the operator
        already knows a runout will not self-heal."""
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, _status(_split_group_trays()), [1], backup_on=False)
        notify.assert_not_awaited()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_matching_keys_say_nothing(self, scheduler, caplog):
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, _status([_tray(1), _tray(3)]), [1])
        notify.assert_not_awaited()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_unmapped_slots_are_skipped(self, scheduler, caplog):
        """``-1`` means 'nothing feeds this requirement' — there is no picked tray."""
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, _status(_split_group_trays()), [-1])
        notify.assert_not_awaited()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_other_extruder_side_is_not_a_peer(self, scheduler, caplog):
        """On dual-nozzle hardware the firmware cannot cross extruders even with backup
        ON, so a near-identical tray on the other side is not a partner to miss."""
        status = SimpleNamespace(
            ams_filament_backup=True,
            ams_extruder_map={"0": 0, "1": 1},
            raw_data={
                "ams": [
                    {"id": 0, "tray": [_tray(1, color=_NEAR_BLACK)]},
                    {"id": 1, "tray": [_tray(0, color=_BLACK, global_tray_id=4)]},
                ]
            },
        )
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, status, [1], dual=True, extruder_map={"0": 0, "1": 1})
        notify.assert_not_awaited()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_exception_inside_the_block_never_reaches_the_dispatch(self, scheduler, caplog):
        """Invariant 10: no farm-side check may crash a dispatch path."""
        with (
            caplog.at_level(logging.DEBUG, logger="backend.app.services.print_scheduler"),
            patch(
                "backend.app.services.print_scheduler._get_printer_backup_context",
                new=AsyncMock(side_effect=RuntimeError("wire exploded")),
            ),
        ):
            await scheduler._warn_backup_group_gap(MagicMock(), 10, [1])
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("backup-partner check failed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_missing_printer_state_stands_down(self, scheduler, caplog):
        with caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"):
            notify = await _run(scheduler, None, [1])
        notify.assert_not_awaited()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]
