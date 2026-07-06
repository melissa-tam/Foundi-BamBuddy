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
    # Farm first-article + failure policy (Phase 3).
    require_first_article: bool = True
    first_article_state: str | None = None
    first_article_reject_reason: str | None = None
    retry_max_per_unit: int = 1
    escalate_consecutive_failures: int = 2
    # median cycle × remaining plates ÷ distinct printers; null when unknown.
    eta_seconds: float | None = None
    printers: list[RunPrinterRef] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


class FirstArticleApprove(BaseModel):
    """Body for ``POST /production-runs/{id}/first-article/approve``."""

    eject_remotely: bool = Field(
        default=False,
        description="When true, dispatch a part-present eject to remove the article; when false, the operator removed it.",
    )


class FirstArticleReject(BaseModel):
    """Body for ``POST /production-runs/{id}/first-article/reject``."""

    reason: str = Field(..., min_length=1, max_length=500)
