from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PrintBatch(Base):
    """Batch grouping for multiple queue items created from the same file.

    Also serves as the farm **production run** (Phase 2) when ``sku_file_id`` is
    set: a run is a batch tied to a SKU file with a unit target and per-run eject
    overrides. Non-farm/legacy batches leave the farm columns NULL.
    """

    __tablename__ = "print_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))

    # Source file (one of these)
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="SET NULL"), nullable=True)
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True
    )

    # Total requested quantity (for display — actual items may differ if cancelled)
    quantity: Mapped[int] = mapped_column(Integer, default=1)

    # Status: active, completed, cancelled, paused (farm runs)
    status: Mapped[str] = mapped_column(String(20), default="active")

    # --- Farm production run fields (Phase 2) ---------------------------------
    # When set, this batch IS a production run of the linked SKU file. SET NULL
    # so deleting the SKU file leaves the run/history intact.
    sku_file_id: Mapped[int | None] = mapped_column(ForeignKey("sku_files.id", ondelete="SET NULL"), nullable=True)
    # Operator's target finished-unit count. NULL for non-farm legacy batches.
    target_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Optional per-run cooldown temperature override (°C) applied to the eject
    # block generation for this run's items, superseding the profile's value.
    cooldown_temp_c_override: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Farm first-article + failure policy (Phase 3) ------------------------
    # When True, the run holds after the first plate (first_article=True item)
    # completes and waits for operator approval before dispatching the rest.
    # Default True for new farm runs; legacy/non-farm batches leave it True but
    # ``first_article_state`` NULL means "not a gated run".
    require_first_article: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # First-article gate state: 'pending_print' (FA queued/printing),
    # 'awaiting_approval' (FA done, operator must approve/reject), 'approved'
    # (rest of the run released) or 'rejected' (run paused). NULL = not a gated
    # run (require_first_article False, or a legacy batch).
    first_article_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Operator's reason for rejecting the first article, surfaced in the UI on a
    # rejected run. Cleared when a new first article is dispatched (resume).
    first_article_reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Per-run failure-policy knobs. Default from the global farm settings at
    # creation; retry_max_per_unit is the max automatic retries per failed unit,
    # escalate_consecutive_failures is the consecutive-failure count on one
    # printer that trips quarantine.
    retry_max_per_unit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    escalate_consecutive_failures: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Serialised plan (JSON) for the plates NOT yet created while the first
    # article is pending — the remaining plates are materialised only when the
    # FA is approved (avoids the scheduler dispatching plate 2 before plate 1 is
    # approved). Persisted (not in-memory) so approval survives a restart. NULL
    # once the plan is consumed or when the run wasn't gated.
    first_article_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Why the run is currently held, when it is (Phase 3, consumed more broadly in
    # Phase 4). Event-fact only — the run's status is still derived/authoritative;
    # this is the human-readable reason surfaced on the run card. Set at hold sites
    # ('operator_stop' when a unit is stopped by the operator — the run stays
    # ACTIVE but shows the hold) and cleared on resume. NULL when not held.
    pause_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # User tracking
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    archive: Mapped["PrintArchive | None"] = relationship()
    library_file: Mapped["LibraryFile | None"] = relationship()
    created_by: Mapped["User | None"] = relationship()
    queue_items: Mapped[list["PrintQueueItem"]] = relationship(back_populates="batch")
    sku_file: Mapped["SkuFile | None"] = relationship()


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.library import LibraryFile  # noqa: E402
from backend.app.models.print_queue import PrintQueueItem  # noqa: E402
from backend.app.models.sku import SkuFile  # noqa: E402
from backend.app.models.user import User  # noqa: E402
