"""Pydantic schemas for the farm production-run API (Phase 2).

A production run is a :class:`~backend.app.models.print_batch.PrintBatch` tied to
a SKU file. All unit/plate counts on the response are DERIVED by query from the
run's queue items — never stored counters.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class RunPrinterRef(BaseModel):
    id: int
    name: str


class RunPrinterState(BaseModel):
    """Live blocked-state summary for one printer a run targets (Phase 4.1).

    Fully DERIVED (never stored): quarantine from the printer row, plate gate /
    model mismatch / connectivity from the live printer manager, stall and
    printer-vision holds from the run's items' ``waiting_reason`` machine codes.
    Only present on ``GET /production-runs/{id}`` — the list stays lean.
    """

    printer_id: int
    name: str
    connected: bool = False
    quarantined: bool = False
    awaiting_plate_clear: bool = False
    model_mismatch: bool = False
    model_mismatch_reason: str | None = None
    # A unit on this printer carries waiting_reason == "printer_offline_stalled".
    stalled: bool = False
    # A unit carries waiting_reason == "plate_not_empty_printer_detected"
    # (the printer's own pre-print vision check found objects on the bed).
    vision_hold: bool = False


class RunUnit(BaseModel):
    """One queue item of a run, as shown on the run detail page (Phase 4.1)."""

    id: int
    status: str
    # 'operator_ui' / 'operator_screen' when the unit was deliberately stopped
    # (Phase 3.1); null for normal terminals.
    stop_source: str | None = None
    waiting_reason: str | None = None
    printer_id: int | None = None
    printer_name: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Retry lineage: which failed unit this one re-covers and the chain depth.
    retry_of_id: int | None = None
    retry_count: int = 0
    filament_short: bool = False
    manual_start: bool = False
    first_article: bool = False
    error_message: str | None = None


class RunCreate(BaseModel):
    """Create a production run.

    Exactly one of ``printer_ids`` (assign to specific printers, round-robin
    across plates) or ``target_model`` (model-based; scheduler assigns to any
    idle matching printer) must be provided. ``eject_profile_id`` falls back to
    the SKU's default when null; a farm run must eject, so both being null is a
    422.
    """

    sku_file_id: int
    target_units: int = Field(..., ge=1)
    printer_ids: list[int] | None = None
    target_model: str | None = None
    eject_profile_id: int | None = None
    cooldown_temp_c_override: float | None = Field(default=None, gt=0, le=100)
    # Farm first-article + failure policy (Phase 3). All optional; numbers fall
    # back to the global farm settings, require_first_article defaults True.
    require_first_article: bool | None = Field(default=None, description="Gate the run on first-article approval")
    retry_max_per_unit: int | None = Field(default=None, ge=0, le=10)
    escalate_consecutive_failures: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def _validate_target(self) -> RunCreate:
        has_printers = bool(self.printer_ids)
        has_model = bool(self.target_model and self.target_model.strip())
        if has_printers and has_model:
            raise ValueError("Provide either printer_ids or target_model, not both")
        if not has_printers and not has_model:
            raise ValueError("Provide either printer_ids or target_model")
        return self


class RunResponse(BaseModel):
    id: int
    name: str
    sku_code: str | None = None
    sku_file_id: int | None = None
    target_units: int | None = None
    units_planned: int
    units_completed: int
    units_failed: int
    plates_total: int
    plates_completed: int
    plates_failed: int
    plates_pending: int
    status: str
    # Why the run is held (Phase 4.1): 'operator', 'operator_stop',
    # 'first_article_rejected', 'no_available_printers' or 'retries_exhausted'
    # (Phase 1). Null when not held.
    pause_reason: str | None = None
    # Pending units system-staged by the low-spool guard (manual_start AND
    # filament_short) vs staged for any other reason (manual_start alone).
    staged_filament_short: int = 0
    staged_other: int = 0
    # True when any printer the run targets is blocked (quarantined, plate gate,
    # model mismatch, offline-stalled, vision hold, or connected-then-lost).
    has_blocked_printers: bool = False
    # Detail-only payloads (GET /production-runs/{id}); null on the list.
    printer_states: list[RunPrinterState] | None = None
    units: list[RunUnit] | None = None
    # Farm first-article + failure policy (Phase 3).
    require_first_article: bool = True
    first_article_state: str | None = None
    first_article_reject_reason: str | None = None
    # First-article inspection payload (Phase 4, F1): populated only while the run
    # is awaiting_approval or rejected; null otherwise. The photo URL is relative
    # (same-origin) — the finished part's newest ``finish_*`` archive photo.
    first_article_photo_url: str | None = None
    first_article_printer_id: int | None = None
    first_article_printer_name: str | None = None
    retry_max_per_unit: int = 1
    escalate_consecutive_failures: int = 2
    # median cycle × remaining plates ÷ distinct printers; null when unknown.
    eta_seconds: float | None = None
    printers: list[RunPrinterRef] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


class FarmPrinterContext(BaseModel):
    """Fleet-scoped "why is this printer on farm work" context (Phase 3, F2).

    One entry per printer assigned to an active/paused production run, surfaced on
    the Printers page so an operator sees why a printer is blocked/idle without
    opening the run detail. Fully DERIVED (never stored): the owning run + SKU plus
    the printer's live/last unit and its ``waiting_reason`` machine code — the same
    vocabulary as ``RunPrinterState`` / ``RunUnit``, no new reason codes.
    """

    printer_id: int
    run_id: int
    run_name: str
    sku_code: str | None = None
    run_status: str
    pause_reason: str | None = None
    # The printer's representative unit: a live printing/pending unit, else the
    # most recent failed one. Null when the printer holds no unit in this run.
    unit_id: int | None = None
    # 'printing' | 'pending' | 'failed' | None.
    unit_status: str | None = None
    # Machine code from the live unit (e.g. 'printer_offline_stalled'); the
    # frontend maps it via the shared waitingReason util. Null when not waiting.
    waiting_reason: str | None = None
    # Last failed unit's error — present ONLY when the printer has no live unit.
    error_message: str | None = None
    # A live pending unit held by the scheduler/operator (manual_start).
    staged: bool = False
    # The hold is the low-spool guard (swap the spool, then release/resume).
    filament_short: bool = False
    # This printer holds the run's first-article unit.
    first_article: bool = False
    first_article_state: str | None = None


class FirstArticleApprove(BaseModel):
    """Body for ``POST /production-runs/{id}/first-article/approve``."""

    eject_remotely: bool = Field(
        default=False,
        description="When true, dispatch a part-present eject to remove the article; when false, the operator removed it.",
    )


class FirstArticleReject(BaseModel):
    """Body for ``POST /production-runs/{id}/first-article/reject``."""

    reason: str = Field(..., min_length=1, max_length=500)
