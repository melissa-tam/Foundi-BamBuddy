"""Tests for the dispatch/eject websocket progress emitters (latency Phase C3/C4).

The event field names are a CONTRACT with the frontend — these tests pin them.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services.eject import progress

pytestmark = pytest.mark.asyncio


@pytest.fixture
def captured(monkeypatch):
    """Capture every message that reaches ws_manager.broadcast (the full emit path,
    including the ts stamp + spawn_background_task scheduling)."""
    msgs: list[dict] = []

    async def _fake_broadcast(message: dict) -> None:
        msgs.append(message)

    monkeypatch.setattr(progress.ws_manager, "broadcast", _fake_broadcast)
    progress._last_upload_emit.clear()
    return msgs


async def _drain():
    # Let the spawned broadcast task(s) run.
    for _ in range(3):
        await asyncio.sleep(0)


async def test_queue_item_status_payload_shape(captured):
    progress.emit_queue_item_status(
        item_id=123, batch_id=45, printer_id=7, status="printing", phase="sent", progress_pct=62.0, detail="x"
    )
    await _drain()

    assert len(captured) == 1
    msg = captured[0]
    assert set(msg) == {"type", "item_id", "batch_id", "printer_id", "status", "phase", "progress_pct", "detail", "ts"}
    assert msg["type"] == "queue_item_status"
    assert msg["item_id"] == 123
    assert msg["batch_id"] == 45
    assert msg["printer_id"] == 7
    assert msg["status"] == "printing"
    assert msg["phase"] == "sent"
    assert msg["progress_pct"] == 62.0
    assert msg["detail"] == "x"
    assert isinstance(msg["ts"], str) and msg["ts"]


async def test_eject_progress_payload_shape(captured):
    progress.emit_eject_progress(printer_id=7, queue_item_id=123, phase="sweeping")
    await _drain()

    assert len(captured) == 1
    msg = captured[0]
    assert set(msg) == {"type", "printer_id", "queue_item_id", "phase", "progress_pct", "detail", "ts"}
    assert msg["type"] == "eject_progress"
    assert msg["printer_id"] == 7
    assert msg["queue_item_id"] == 123
    assert msg["phase"] == "sweeping"
    assert msg["progress_pct"] is None
    assert msg["detail"] is None


async def test_uploading_throttled_within_one_hz(captured, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock["t"])

    # Burst of uploading events at the SAME instant → only the first passes.
    for pct in (10.0, 25.0, 40.0):
        progress.emit_queue_item_status(
            item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=pct
        )
    await _drain()
    assert [m["progress_pct"] for m in captured] == [10.0]

    # >1 s later, uploading passes again.
    clock["t"] += 1.5
    progress.emit_queue_item_status(
        item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=80.0
    )
    await _drain()
    assert [m["progress_pct"] for m in captured] == [10.0, 80.0]


async def test_phase_changes_always_pass_and_reset_window(captured, monkeypatch):
    clock = {"t": 500.0}
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock["t"])

    # uploading (passes), uploading (throttled), then a phase change ALWAYS passes...
    progress.emit_queue_item_status(
        item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=5.0
    )
    progress.emit_queue_item_status(
        item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=6.0
    )
    progress.emit_queue_item_status(item_id=1, batch_id=None, printer_id=7, status="printing", phase="sent")
    # ...and resets the throttle window so the next uploading passes immediately.
    progress.emit_queue_item_status(
        item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=7.0
    )
    await _drain()

    phases = [(m["phase"], m.get("progress_pct")) for m in captured]
    assert phases == [("uploading", 5.0), ("sent", None), ("uploading", 7.0)]


async def test_throttle_is_per_key(captured, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock["t"])

    # Same instant, DIFFERENT items — both uploading pass (per-key throttle).
    progress.emit_queue_item_status(
        item_id=1, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=1.0
    )
    progress.emit_queue_item_status(
        item_id=2, batch_id=None, printer_id=7, status="pending", phase="uploading", progress_pct=2.0
    )
    # eject throttle keys on printer, independent of the item keys above.
    progress.emit_eject_progress(printer_id=7, queue_item_id=None, phase="uploading", progress_pct=3.0)
    await _drain()
    assert sorted(m["progress_pct"] for m in captured) == [1.0, 2.0, 3.0]


async def test_phase_for_observed_state():
    assert progress.phase_for_observed_state("PREPARE") == "preparing"
    assert progress.phase_for_observed_state("SLICING") == "preparing"
    assert progress.phase_for_observed_state("RUNNING") == "printing"
    assert progress.phase_for_observed_state("FINISH") is None
    assert progress.phase_for_observed_state(None) is None


async def test_no_running_loop_is_silent_noop(monkeypatch):
    # Called from a sync context (no loop): emit must not raise and must not schedule.
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(progress.ws_manager, "broadcast", _boom)
    # Run in a thread with no event loop.
    import threading

    err = []

    def _run():
        try:
            progress.emit_eject_progress(printer_id=1, queue_item_id=None, phase="building")
        except Exception as exc:  # noqa: BLE001
            err.append(exc)

    t = threading.Thread(target=_run)
    t.start()
    t.join()
    assert not err
    assert called["n"] == 0
