"""The dispatcher may only print a unit it actually CLAIMED.

``_start_print`` loads the queue item, then spends seconds inside an FTPS upload
before it writes the dispatch. An operator cancel landing in that gap used to be
overwritten straight back to ``printing`` by four ORM assignments, and the print
command went out anyway — the mirror of the 2026-08-20 run-abort lost update
(010-H2S run 112 / item 865), seen from the scheduler's side.

The write is now ``queue_transitions.claim_pending_for_dispatch``, whose WHERE
carries the ``pending`` precondition. These tests pin the two things that follow
from a refused claim: **no print command is sent**, and the item is left exactly
as the actor that moved it left it (no failure, no retry burn, no quarantine
contribution).

The suite's engine is a shared-connection in-memory SQLite, so the "operator"
cancel here runs on the dispatcher's own session — the transition's cross-session
correctness is pinned on a file-backed database in
``test_queue_transitions.py::TestDispatchClaimRace``. What this module owns is
``_start_print``'s behaviour once the claim answers no.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import print_scheduler as ps_module
from backend.app.services.print_scheduler import scheduler
from backend.app.services.printer_manager import printer_manager
from backend.app.services.queue_transitions import cancel_pending_items
from backend.app.utils.filename import derive_remote_filename

pytestmark = pytest.mark.asyncio


async def _seed_dispatchable(db, tmp_path, *, name="DC"):
    """A pending, archive-backed queue item whose source file exists on disk."""
    printer = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(printer)
    await db.flush()

    source = tmp_path / f"{name}.gcode.3mf"
    source.write_bytes(b"PK\x03\x04 not really a 3mf")
    archive = PrintArchive(
        printer_id=printer.id,
        filename=source.name,
        file_path=str(source),  # absolute → base_dir / this yields this
        file_size=source.stat().st_size,
        status="completed",
    )
    db.add(archive)
    await db.flush()

    item = PrintQueueItem(printer_id=printer.id, archive_id=archive.id, status="pending", plate_id=1, position=1)
    db.add(item)
    await db.commit()
    # The remote path the dispatch uploads to — and must clean up if it cannot claim.
    return printer, item, f"/{derive_remote_filename(source.name)}"


@contextlib.contextmanager
def _dispatch_env(*, upload=None, delete=None):
    """Patch everything ``_start_print`` touches between the USB gate and the claim.

    ``upload`` overrides the FTPS upload coroutine — the hook the cancel test uses
    to move the row while the dispatcher is "uploading". ``delete`` overrides the
    FTPS delete, so a test can make the post-refusal cleanup fail. Yields the mocks
    worth asserting on; ``start`` is ``printer_manager.start_print``, i.e. the print
    command itself, and ``delete`` is the USB cleanup.
    """
    start = MagicMock(return_value=True)
    upload_mock = AsyncMock(return_value=True) if upload is None else AsyncMock(side_effect=upload)
    delete_mock = AsyncMock(return_value=True) if delete is None else AsyncMock(side_effect=delete)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(printer_manager, "is_connected", return_value=True))
        # No ``state`` attribute → pre_state is None → no start watchdog task.
        stack.enter_context(
            patch.object(printer_manager, "get_status", MagicMock(return_value=SimpleNamespace(sdcard=True)))
        )
        stack.enter_context(patch.object(printer_manager, "get_client", MagicMock(return_value=None)))
        stack.enter_context(patch.object(printer_manager, "request_status_update", MagicMock(return_value=True)))
        stack.enter_context(patch.object(printer_manager, "set_awaiting_plate_clear", MagicMock()))
        stack.enter_context(patch.object(printer_manager, "start_print", start))
        stack.enter_context(
            patch.object(ps_module, "get_ftp_retry_settings", AsyncMock(return_value=(False, 3, 1.0, 30.0)))
        )
        stack.enter_context(patch.object(ps_module, "delete_file_async", delete_mock))
        stack.enter_context(patch.object(ps_module, "upload_file_async", upload_mock))
        stack.enter_context(patch.object(ps_module, "with_ftp_retry", AsyncMock(return_value=True)))
        stack.enter_context(patch.object(ps_module, "cache_3mf_download", MagicMock()))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_started", AsyncMock()))
        stack.enter_context(patch.object(ps_module.notification_service, "on_queue_job_failed", AsyncMock()))
        stack.enter_context(patch("backend.app.main.register_expected_print", MagicMock()))
        stack.enter_context(patch.object(scheduler, "_power_off_if_needed", AsyncMock()))
        yield SimpleNamespace(start=start, upload=upload_mock, delete=delete_mock)


class TestDispatchClaimHappyPath:
    async def test_a_claimed_item_commits_printing_then_sends_the_command(self, db_session, tmp_path):
        printer, item, remote_path = await _seed_dispatchable(db_session, tmp_path, name="DCOK")

        with _dispatch_env() as m:
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.start.assert_called_once()
        assert m.start.call_args.kwargs["ams_mapping"] == [1, -1]

        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item.id))).scalar_one()
        assert row.status == "printing"
        assert row.started_at is not None
        assert row.waiting_reason is None
        assert row.ams_mapping == "[1, -1]"
        # Only the routine pre-upload clear — a CLAIMED dispatch leaves its file
        # in place, which is what makes the refusal-path delete meaningful.
        assert m.delete.await_count == 1
        assert m.delete.await_args.args[2] == remote_path

    async def test_the_in_memory_item_is_synchronised_after_the_claim(self, db_session, tmp_path):
        """The UPDATE is ``synchronize_session=False``, so the instance the rest of
        ``_start_print`` keeps using (``dispatch_subtask_id`` writes through it) has
        to be refreshed — otherwise it would still read 'pending'."""
        printer, item, _remote_path = await _seed_dispatchable(db_session, tmp_path, name="DCSY")

        with _dispatch_env():
            await scheduler._start_print(db_session, item)

        assert item.status == "printing"
        assert item.started_at is not None


class TestCancelDuringUpload:
    async def test_a_cancel_in_the_upload_gap_stops_the_print_command(self, db_session, tmp_path, caplog):
        """The incident shape: the operator cancels while the file is uploading."""
        printer, item, remote_path = await _seed_dispatchable(db_session, tmp_path, name="DCRC")
        item_id = item.id

        async def _cancel_mid_upload(*_args, **_kwargs):
            # Exactly what every operator cancel path now does (run abort, batch
            # cancel, single-item cancel) — and it COMMITS, as a request does.
            assert await cancel_pending_items(db_session, item_ids=[item_id]) == [item_id]
            await db_session.commit()
            return True

        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"),
            _dispatch_env(upload=_cancel_mid_upload) as m,
        ):
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.upload.assert_awaited_once()
        m.start.assert_not_called()  # the print command never went out

        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
        assert row.status == "cancelled"
        assert row.completed_at is not None
        assert row.started_at is None
        assert row.ams_mapping is None  # the dispatcher's decision was not recorded

        # The upload is undone: a cancelled unit's file left on the USB is one
        # screen-tap away from a foreign print. Twice — the routine pre-upload
        # clear, then the cleanup — both against the same remote path.
        assert m.delete.await_count == 2
        assert m.delete.await_args.args[2] == remote_path

        assert any(
            "left 'pending' during dispatch" in r.message
            and "cancelled" in r.message
            and f"uploaded file {remote_path.lstrip('/')} removed from printer" in r.message
            for r in caplog.records
        ), caplog.text

    async def test_a_refused_claim_never_fails_the_item(self, db_session, tmp_path, caplog):
        """Not our row to mark terminal: no 'failed', no retry burn, no exception.

        The claim is stubbed to refuse so the item's PRE-claim status is what the
        assertion sees — proof the dispatcher writes nothing at all on a refusal.
        """
        printer, item, remote_path = await _seed_dispatchable(db_session, tmp_path, name="DCNF")
        item_id = item.id

        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"),
            _dispatch_env() as m,
            patch.object(ps_module, "claim_pending_for_dispatch", AsyncMock(return_value=False)),
        ):
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.start.assert_not_called()
        row = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))).scalar_one()
        assert row.status == "pending"
        assert row.error_message is None
        assert row.completed_at is None
        assert m.delete.await_args.args[2] == remote_path
        assert any("print command NOT sent" in r.message for r in caplog.records), caplog.text

    async def test_a_failed_cleanup_is_reported_not_raised(self, db_session, tmp_path, caplog):
        """Best-effort: the row is already someone else's, so a dead FTPS session
        must not turn a lost race into a crash — it changes the warning's wording."""
        printer, item, remote_path = await _seed_dispatchable(db_session, tmp_path, name="DCDF")
        item_id = item.id

        async def _delete_explodes(*_args, **_kwargs):
            raise OSError("ftps session gone")

        with (
            caplog.at_level(logging.WARNING, logger="backend.app.services.print_scheduler"),
            _dispatch_env(delete=_delete_explodes) as m,
            patch.object(ps_module, "claim_pending_for_dispatch", AsyncMock(return_value=False)),
        ):
            await scheduler._start_print(db_session, item, ams_mapping=[1, -1])

        m.start.assert_not_called()
        assert (await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))) == "pending"
        assert any("COULD NOT BE REMOVED from printer" in r.message for r in caplog.records), caplog.text
