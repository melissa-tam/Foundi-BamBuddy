"""Unit tests for the filament-deficit pre-dispatch check (#1496).

The check is the single source of truth that both ``POST /queue/{id}/start``
and the dispatch scheduler call before sending a print to the printer. Pin
the contract for the cases that matter:

* Internal-inventory mode: shortfall + sufficient + no assignment.
* AMS-mapping gating: a missing mapping means "not yet decided, skip".
* Disabled-warnings setting + missing printer (model-based item) + no
  source 3MF all short-circuit to "no deficit".
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.filament_deficit import (
    FilamentDeficit,
    compute_deficit_for_queue_item,
)


def _write_3mf(file_path: Path, filaments: list[dict]) -> None:
    """Minimal 3MF that ``extract_filament_requirements`` can parse (flat shape)."""
    body = "".join(
        f'<filament id="{f["id"]}" type="{f["type"]}" color="{f["color"]}" '
        f'used_g="{f["used_g"]}" tray_info_idx="{f.get("tray_info_idx", "")}"/>'
        for f in filaments
    )
    config = f'<?xml version="1.0" encoding="utf-8"?><config>{body}</config>'
    with zipfile.ZipFile(file_path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", config)


async def _setup_archive_3mf(db_session, tmp_path: Path, filaments: list[dict]) -> PrintArchive:
    """Create a 3MF on disk and a PrintArchive row pointing at it."""
    file_name = "model.3mf"
    file_path = tmp_path / file_name
    _write_3mf(file_path, filaments)
    archive = PrintArchive(
        filename=file_name,
        print_name="Test",
        # The helper resolves via app_settings.base_dir / file_path, but
        # storing the absolute path on the model also works because
        # ``Path / abs`` collapses to the absolute side.
        file_path=str(file_path),
        file_size=file_path.stat().st_size,
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _spool(
    db_session,
    *,
    label_weight: int,
    weight_used: float,
    color: str = "#000000",
    slicer_filament: str | None = None,
) -> Spool:
    spool = Spool(
        material="PLA",
        label_weight=label_weight,
        weight_used=weight_used,
        rgba=color,
        slicer_filament=slicer_filament,
    )
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _assign(db_session, *, printer_id: int, spool_id: int, ams_id: int = 0, tray_id: int = 0) -> None:
    db_session.add(
        SpoolAssignment(
            spool_id=spool_id,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
        )
    )
    await db_session.commit()


async def _queue_item(
    db_session,
    *,
    printer_id: int | None,
    archive: PrintArchive | None,
    ams_mapping: list[int] | None,
    plate_id: int | None = None,
) -> PrintQueueItem:
    item = PrintQueueItem(
        printer_id=printer_id,
        archive_id=archive.id if archive else None,
        ams_mapping=json.dumps(ams_mapping) if ams_mapping is not None else None,
        plate_id=plate_id,
        status="pending",
        manual_start=True,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item, ["archive", "library_file"])
    return item


class TestFilamentDeficit:
    @pytest.mark.asyncio
    async def test_returns_deficit_when_spool_too_light(self, db_session, printer_factory, tmp_path):
        """Spool with 30g remaining for a 100g print → one deficit row."""
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        spool = await _spool(db_session, label_weight=1000, weight_used=970.0)  # 30g left
        await _assign(db_session, printer_id=printer.id, spool_id=spool.id, ams_id=0, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert len(deficit) == 1
        assert isinstance(deficit[0], FilamentDeficit)
        assert deficit[0].slot_id == 1
        assert deficit[0].required_grams == 100.0
        assert deficit[0].remaining_grams == 30.0
        assert deficit[0].filament_type == "PLA"

    @pytest.mark.asyncio
    async def test_returns_empty_when_spool_has_enough(self, db_session, printer_factory, tmp_path):
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        spool = await _spool(db_session, label_weight=1000, weight_used=200.0)  # 800g left
        await _assign(db_session, printer_id=printer.id, spool_id=spool.id, ams_id=0, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_ams_mapping_missing(self, db_session, printer_factory, tmp_path):
        """No mapping yet = scheduler hasn't decided which slot maps where."""
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=None)

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_printer_assigned(self, db_session, tmp_path):
        """Model-based queue items with no resolved printer_id can't be checked."""
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        item = await _queue_item(db_session, printer_id=None, archive=archive, ams_mapping=[0])

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_warnings_disabled(self, db_session, printer_factory, tmp_path):
        """Honour the disable_filament_warnings setting (#720 toggle)."""
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        spool = await _spool(db_session, label_weight=1000, weight_used=970.0)
        await _assign(db_session, printer_id=printer.id, spool_id=spool.id)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])
        db_session.add(Settings(key="disable_filament_warnings", value="true"))
        await db_session.commit()

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_assignment(self, db_session, printer_factory, tmp_path):
        """Mapping points at a slot with no spool assigned → silent, not blocked."""
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [{"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"}],
        )
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_3mf_missing(self, db_session, printer_factory):
        printer = await printer_factory()
        archive = PrintArchive(
            filename="ghost.3mf",
            file_path="/nonexistent/ghost.3mf",
            file_size=0,
            status="completed",
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        deficit = await compute_deficit_for_queue_item(db_session, item)

        assert deficit == []

    @pytest.mark.asyncio
    async def test_multi_slot_only_shorted_slot_returned(self, db_session, printer_factory, tmp_path):
        """One slot fine, one short — only the short slot is in the result."""
        printer = await printer_factory()
        archive = await _setup_archive_3mf(
            db_session,
            tmp_path,
            [
                {"id": "1", "type": "PLA", "color": "#FFFFFF", "used_g": "100.0"},
                {"id": "2", "type": "PETG", "color": "#000000", "used_g": "80.0"},
            ],
        )
        plenty = await _spool(db_session, label_weight=1000, weight_used=100.0)  # 900g
        shorted = await _spool(db_session, label_weight=1000, weight_used=950.0)  # 50g
        await _assign(db_session, printer_id=printer.id, spool_id=plenty.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=shorted.id, ams_id=0, tray_id=1)
        item = await _queue_item(
            db_session,
            printer_id=printer.id,
            archive=archive,
            ams_mapping=[0, 1],  # slot 1 -> tray 0, slot 2 -> tray 1
        )

        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert [d.slot_id for d in deficit] == [2]
        assert deficit[0].remaining_grams == 50.0
        assert deficit[0].required_grams == 80.0


class TestFilamentDeficitBackupAware:
    """#1762 — when AMS Filament Backup is ON, pool remaining grams by LIVE
    TRAY identity (``tray_info_idx``/``tray_type`` + colour) across all loaded
    trays on the printer, within the same extruder side on dual-nozzle models.

    The firmware pools by the tray's *configured* filament and switches on
    physical runout — it does NOT care whether the software holds an inventory
    binding for the spool. So pooling keys on the live tray, and a loaded tray
    whose grams can't be priced (an unbound / no-RFID spool) makes its
    identity's pool open-ended → requirements for it are never blocked.
    """

    @staticmethod
    def _patch_status(
        *,
        printer_id: int,
        backup_on: bool,
        ams_extruder_map: dict | None = None,
        model: str | None = None,
        trays: list[tuple] | None = None,
    ):
        """Patch ``printer_manager.get_status`` + ``get_model`` for the test.

        ``trays`` is a list of ``(ams_id, tray_id, tray_type, tray_info_idx,
        tray_color)`` describing the LIVE AMS trays the firmware reports; they
        are grouped into the ``raw_data['ams']`` shape the real MQTT layer
        produces (string ids, one unit per ams_id). Passing ``None`` builds a
        state with no AMS structure at all.
        """
        from types import SimpleNamespace
        from unittest.mock import patch as _patch

        raw_data: dict = {}
        if trays is not None:
            units: dict[int, dict] = {}
            for ams_id, tray_id, tray_type, info_idx, color in trays:
                unit = units.setdefault(ams_id, {"id": str(ams_id), "tray": []})
                unit["tray"].append(
                    {
                        "id": str(tray_id),
                        "tray_type": tray_type,
                        "tray_info_idx": info_idx,
                        "tray_color": color,
                    }
                )
            raw_data["ams"] = list(units.values())

        fake_state = SimpleNamespace(
            ams_filament_backup=backup_on if backup_on is not None else None,
            ams_extruder_map=ams_extruder_map or {},
            raw_data=raw_data,
        )

        return [
            _patch(
                "backend.app.services.printer_manager.printer_manager.get_status",
                lambda pid: fake_state if pid == printer_id else None,
            ),
            _patch(
                "backend.app.services.printer_manager.printer_manager.get_model",
                lambda pid: model if pid == printer_id else None,
            ),
        ]

    @staticmethod
    async def _run(db_session, item, *, printer_id, backup_on, model=None, ams_extruder_map=None, trays=None):
        """Run ``compute_deficit_for_queue_item`` under a patched live status."""
        patches = TestFilamentDeficitBackupAware._patch_status(
            printer_id=printer_id,
            backup_on=backup_on,
            model=model,
            ams_extruder_map=ams_extruder_map,
            trays=trays,
        )
        with patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")):
            for p in patches:
                p.start()
            try:
                return await compute_deficit_for_queue_item(db_session, item)
            finally:
                for p in patches:
                    p.stop()

    # ---- (a) live incident: unbound same-identity tray opens the pool --------
    @pytest.mark.asyncio
    async def test_backup_on_unbound_tray_same_identity_covers_pool(self, db_session, printer_factory, tmp_path):
        """Two bound spools (250 g + 100 g, GFG02/black) plus a loaded UNBOUND
        no-RFID tray of the same identity. Bound pool (350 g) is under the
        400 g need, but the unbound tray makes the identity open-ended → no
        deficit (firmware will switch to it on runout)."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "400.0"}]
        )
        bound250 = await _spool(db_session, label_weight=1000, weight_used=750.0)  # 250 g
        bound100 = await _spool(db_session, label_weight=1000, weight_used=900.0)  # 100 g
        await _assign(db_session, printer_id=printer.id, spool_id=bound250.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=bound100.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        # Tray (0,2) is a loaded no-RFID spool: live identity, NO binding.
        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "PETG", "GFG02", "000000FF"),
            (0, 2, "PETG", "GFG02", "000000FF"),
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert deficit == []

    # ---- (b) unbound tray of a DIFFERENT colour cannot back the pool ---------
    @pytest.mark.asyncio
    async def test_backup_on_unbound_tray_different_colour_still_short(self, db_session, printer_factory, tmp_path):
        """Same as (a) but the unbound tray is WHITE — a different identity, so
        it does not open the black pool. Bound black pool 350 g < 400 g →
        deficit stands."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "400.0"}]
        )
        bound250 = await _spool(db_session, label_weight=1000, weight_used=750.0)  # 250 g
        bound100 = await _spool(db_session, label_weight=1000, weight_used=900.0)  # 100 g
        await _assign(db_session, printer_id=printer.id, spool_id=bound250.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=bound100.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "PETG", "GFG02", "000000FF"),
            (0, 2, "PETG", "GFG02", "FFFFFFFF"),  # unbound but WHITE
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert len(deficit) == 1
        assert deficit[0].slot_id == 1

    # ---- (c) all bound, pool sufficient (+ colour-alpha normalisation) -------
    @pytest.mark.asyncio
    async def test_backup_on_all_bound_pool_sufficient(self, db_session, printer_factory, tmp_path):
        """All trays bound, same identity, pool 550 g ≥ 400 g → no deficit. The
        two trays' colours differ only by an explicit alpha byte (``000000`` vs
        ``000000FF``) — normalisation must treat them as one identity."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "400.0"}]
        )
        b1 = await _spool(db_session, label_weight=1000, weight_used=750.0)  # 250 g
        b2 = await _spool(db_session, label_weight=1000, weight_used=700.0)  # 300 g
        await _assign(db_session, printer_id=printer.id, spool_id=b1.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=b2.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000"),  # 6-char
            (0, 1, "PETG", "GFG02", "000000FF"),  # 8-char, same RGB
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert deficit == []

    # ---- (d) all bound, pool insufficient, no open-ended tray → deficit ------
    @pytest.mark.asyncio
    async def test_backup_on_all_bound_pool_insufficient(self, db_session, printer_factory, tmp_path):
        """All trays bound, same identity, pool 300 g < 400 g, and no unbound
        peer to open the pool → deficit fires."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "400.0"}]
        )
        b1 = await _spool(db_session, label_weight=1000, weight_used=900.0)  # 100 g
        b2 = await _spool(db_session, label_weight=1000, weight_used=800.0)  # 200 g
        await _assign(db_session, printer_id=printer.id, spool_id=b1.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=b2.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "PETG", "GFG02", "000000FF"),
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert len(deficit) == 1
        assert deficit[0].slot_id == 1

    # ---- (e) backup OFF → per-slot accounting unchanged ----------------------
    @pytest.mark.asyncio
    async def test_backup_off_falls_back_to_per_slot_accounting(self, db_session, printer_factory, tmp_path):
        """Backup OFF: identical trays to a pool-covered case, but the per-slot
        path (phase 2) ignores pooling → the short mapped slot deficits."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "200.0"}]
        )
        short = await _spool(db_session, label_weight=1000, weight_used=990.0)  # 10 g
        peer = await _spool(db_session, label_weight=1000, weight_used=500.0)  # 500 g
        await _assign(db_session, printer_id=printer.id, spool_id=short.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=peer.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "PETG", "GFG02", "000000FF"),
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=False, model="X1C", trays=trays)
        assert len(deficit) == 1
        assert deficit[0].remaining_grams == 10.0

    # ---- (f) empty / blank trays never create an identity --------------------
    @pytest.mark.asyncio
    async def test_backup_on_blank_tray_creates_no_identity(self, db_session, printer_factory, tmp_path):
        """A blank tray (no ``tray_type``) must NOT create an identity — so it
        can't open an undetermined pool. Bound 100 g < 400 g and the blank
        peer is ignored → deficit fires."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "400.0"}]
        )
        bound = await _spool(db_session, label_weight=1000, weight_used=900.0)  # 100 g
        await _assign(db_session, printer_id=printer.id, spool_id=bound.id, ams_id=0, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "", "", ""),  # blank slot — no identity
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert len(deficit) == 1
        assert deficit[0].slot_id == 1

    # ---- (g) dual-extruder: same identity across sides does NOT pool ---------
    @pytest.mark.asyncio
    async def test_backup_on_dual_extruder_scopes_pool_per_side(self, db_session, printer_factory, tmp_path):
        """Dual-extruder (H2D): the same-identity peer tray lives on the OTHER
        extruder, which firmware can't reach — so it doesn't count. Mapped
        slot's 10 g < 200 g → deficit."""
        printer = await printer_factory(model="O1D")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "200.0"}]
        )
        short = await _spool(db_session, label_weight=1000, weight_used=990.0)  # 10 g, extruder 0
        peer_other = await _spool(db_session, label_weight=1000, weight_used=500.0)  # 500 g, extruder 1
        await _assign(db_session, printer_id=printer.id, spool_id=short.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=peer_other.id, ams_id=1, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),  # extruder 0
            (1, 0, "PETG", "GFG02", "000000FF"),  # extruder 1 — unreachable
        ]
        deficit = await self._run(
            db_session,
            item,
            printer_id=printer.id,
            backup_on=True,
            model="O1D",
            ams_extruder_map={"0": 0, "1": 1},
            trays=trays,
        )
        assert len(deficit) == 1
        assert deficit[0].slot_id == 1

    # ---- (h) no printer state → backup context False → per-slot --------------
    @pytest.mark.asyncio
    async def test_no_printer_state_falls_back_to_per_slot(self, db_session, printer_factory, tmp_path):
        """When ``get_status`` returns None the backup context is False, so the
        per-slot path runs — the short mapped slot deficits."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "200.0"}]
        )
        short = await _spool(db_session, label_weight=1000, weight_used=990.0)  # 10 g
        await _assign(db_session, printer_id=printer.id, spool_id=short.id, ams_id=0, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        with (
            patch("backend.app.services.filament_deficit.app_settings.base_dir", Path("/")),
            patch("backend.app.services.printer_manager.printer_manager.get_status", lambda pid: None),
        ):
            deficit = await compute_deficit_for_queue_item(db_session, item)

        assert len(deficit) == 1
        assert deficit[0].remaining_grams == 10.0

    # ---- (i) mapped slot with no live tray identity → strict per-slot --------
    @pytest.mark.asyncio
    async def test_backup_on_mapped_slot_no_live_identity_strict_per_slot(self, db_session, printer_factory, tmp_path):
        """Backup ON, but the mapped slot's live tray is blank (spool pulled)
        while its inventory binding persists. No identity → strict per-slot
        check → the bound 10 g vs 200 g need deficits."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "200.0"}]
        )
        short = await _spool(db_session, label_weight=1000, weight_used=990.0)  # 10 g, binding persists
        await _assign(db_session, printer_id=printer.id, spool_id=short.id, ams_id=0, tray_id=0)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [(0, 0, "", "", "")]  # live tray at the mapped slot is blank
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert len(deficit) == 1
        assert deficit[0].slot_id == 1
        assert deficit[0].remaining_grams == 10.0

    # ---- (j) preset-less BOUND spools now pool by tray identity --------------
    @pytest.mark.asyncio
    async def test_backup_on_presetless_bound_spools_pool_by_tray_identity(
        self, db_session, printer_factory, tmp_path
    ):
        """The old unique-key rule is gone: two BOUND spools with no
        ``slicer_filament`` preset, sitting in live trays of the same identity,
        now pool (510 g ≥ 200 g) → no deficit."""
        printer = await printer_factory(model="X1C")
        archive = await _setup_archive_3mf(
            db_session, tmp_path, [{"id": "1", "type": "PETG", "color": "#000000", "used_g": "200.0"}]
        )
        a = await _spool(db_session, label_weight=1000, weight_used=990.0)  # 10 g, NO preset
        b = await _spool(db_session, label_weight=1000, weight_used=500.0)  # 500 g, NO preset
        await _assign(db_session, printer_id=printer.id, spool_id=a.id, ams_id=0, tray_id=0)
        await _assign(db_session, printer_id=printer.id, spool_id=b.id, ams_id=0, tray_id=1)
        item = await _queue_item(db_session, printer_id=printer.id, archive=archive, ams_mapping=[0])

        trays = [
            (0, 0, "PETG", "GFG02", "000000FF"),
            (0, 1, "PETG", "GFG02", "000000FF"),
        ]
        deficit = await self._run(db_session, item, printer_id=printer.id, backup_on=True, model="X1C", trays=trays)
        assert deficit == []
