from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class PrintQueueItem(Base):
    """Print queue item for scheduled/queued prints."""

    __tablename__ = "print_queue"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Links
    printer_id: Mapped[int | None] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=True)
    # Target printer model for model-based assignment (mutually exclusive with printer_id)
    # When set, scheduler assigns to any idle printer of matching model
    target_model: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Target location filter for model-based assignment (only used with target_model)
    # When set, only printers in this location are considered
    target_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Required filament types for model-based assignment (JSON array, e.g., '["PLA", "PETG"]')
    # Used by scheduler to validate printer has compatible filaments loaded
    required_filament_types: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Waiting reason - explains why a model-based job hasn't started yet
    # Set by scheduler when no matching printer is available
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Either archive_id OR library_file_id must be set (archive created at print start from library file)
    archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id", ondelete="CASCADE"), nullable=True)
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="CASCADE"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("print_batches.id", ondelete="SET NULL"), nullable=True)
    # Farm auto-eject: when set, the scheduler generates a cooldown→sweep→park
    # block from this profile and injects it as the machine-end snippet
    # (superseding the global per-model end snippet) so the part is cleared off
    # the plate before the next unit dispatches. SET NULL so deleting a profile
    # leaves historical/queued items intact (they revert to no auto-eject).
    eject_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("eject_profiles.id", ondelete="SET NULL"), nullable=True
    )

    # Scheduling
    position: Mapped[int] = mapped_column(Integer, default=0)  # Queue order
    scheduled_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # None = ASAP
    manual_start: Mapped[bool] = mapped_column(Boolean, default=False)  # Requires manual trigger to start

    # Conditions
    require_previous_success: Mapped[bool] = mapped_column(Boolean, default=False)

    # Power management
    auto_off_after: Mapped[bool] = mapped_column(Boolean, default=False)  # Power off printer after print

    # AMS mapping: JSON array of global tray IDs for each filament slot
    # Format: "[5, -1, 2, -1]" where position = slot_id-1, value = global tray ID (-1 = unused)
    ams_mapping: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Filament overrides for model-based assignment: JSON array of override objects
    # Format: '[{"slot_id": 1, "type": "PLA", "color": "#FFFFFF"}]'
    # Only slots with overrides are included (sparse). null = use original 3MF values.
    filament_overrides: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Plate ID for multi-plate 3MF files (1-indexed, None = auto-detect/plate 1)
    plate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Farm first-article + retry policy (Phase 3) --------------------------
    # True for the run's first-article plate: the eject block is NOT injected (the
    # part stays on the plate for inspection) and the plate-clear monitor never
    # auto-clears the gate for it. Cleared (False) on every other plate.
    first_article: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # True for a dry-run eject dispatch: a thermal-less empty-bed sweep test that
    # deposits NOTHING on the plate by construction. Lets the terminal-status
    # handler treat a stopped/finished dry run as a no-deposit finish (no
    # plate-clear gate, not a failure) regardless of any progress the non-print
    # gcode reported. Cleared (False) on every ordinary print. Mirrors first_article.
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Automatic-retry generation for this unit: 0 = original attempt, N = the Nth
    # retry after a failure. Bounded by the run's retry_max_per_unit.
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Lineage link to the failed item this row is a retry of. Makes retry
    # creation idempotent (exactly one retry per failure event) and traceable.
    # SET NULL so deleting the original leaves the retry standing. UNIQUE (NULLs
    # allowed) so the check-then-insert retry guard is backed by a DB constraint —
    # a race that tried to create two retries for one failure fails the second
    # insert instead of silently double-printing (#C7 idempotency, Phase 1).
    retry_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True, unique=True
    )

    # Subtask id minted for THIS unit's dispatch (BambuMQTTClient.last_dispatch_subtask_id
    # at start_print time; the printer echoes it back in push_status). Stamped
    # post-dispatch by the scheduler and used by services.farm_correlation to bind a
    # terminal MQTT status to the exact queue item that produced it — instead of the
    # printer_id-only lookup that misattributed a foreign/local print to a farm unit
    # (S4/P1-A). NULL for rows dispatched before this field existed (upgrade day) and
    # for items never dispatched.
    dispatch_subtask_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # How this unit reached a terminal 'cancelled': 'operator_ui' (Stop pressed in
    # the Bambuddy queue UI) or 'operator_screen' (stopped on the printer's own
    # touchscreen — detected from the firmware's cancel-echo HMS codes). NULL for
    # a genuine failure, a normal completion, or a reconcile-synthesised interruption
    # (unknown cause). Drives the farm policy: an attributed operator stop takes NO
    # auto-retry and does NOT count toward quarantine (Phase 3).
    stop_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Shortest-job-first scheduling
    print_time_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)  # Cached from archive/library
    been_jumped: Mapped[bool] = mapped_column(Boolean, default=False)  # Starvation guard for SJF

    # Auto-print G-code injection (#422)
    gcode_injection: Mapped[bool] = mapped_column(Boolean, default=False)

    # H2C dual-nozzle-rack slicer pick preservation (#1780). BambuStudio's
    # project_file MQTT command for rack-swap-capable models (O1C2 today)
    # carries per-filament physical nozzle position IDs in `nozzle_mapping`,
    # forwarded verbatim through the queue and replayed by the dispatcher so
    # the firmware honours the user's pick instead of falling back to
    # "last matching nozzle type" auto-pick. Stored as opaque JSON string
    # (list[int]); NULL on every other model. `nozzles_info` is a deprecated
    # column from the original #1780 attempt — kept nullable so old rows still
    # load; never written to or read from.
    nozzle_mapping: Mapped[str | None] = mapped_column(Text, nullable=True)
    nozzles_info: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Printer-card direct uploads create transient library rows. When this is
    # true, the scheduler deletes the source row/files after archiving a copy.
    cleanup_library_after_dispatch: Mapped[bool] = mapped_column(Boolean, default=False)

    # Print options
    bed_levelling: Mapped[bool] = mapped_column(Boolean, default=True)
    flow_cali: Mapped[bool] = mapped_column(Boolean, default=False)
    vibration_cali: Mapped[bool] = mapped_column(Boolean, default=True)
    layer_inspect: Mapped[bool] = mapped_column(Boolean, default=False)
    timelapse: Mapped[bool] = mapped_column(Boolean, default=False)
    use_ams: Mapped[bool] = mapped_column(Boolean, default=True)
    # Nozzle offset calibration — dual-nozzle printers only, MQTT-gated (#1682)
    nozzle_offset_cali: Mapped[bool] = mapped_column(Boolean, default=True)

    # Status: pending, printing, completed, failed, skipped, cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")

    # Cleared by the per-printer "Resume after failure" action (#1818) so the
    # scheduler's `_check_previous_success` lookback skips this row. Without
    # this, a single `failed` or `aborted` print poisoned every later
    # `require_previous_success` item on the same printer forever — the
    # lookback excluded `skipped` but had no way to dismiss the originating
    # failure. The flag is per-item, not per-printer, so a fresh failure
    # after a resume re-gates downstream items independently.
    gate_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set by the dispatch scheduler when the assigned spool can't satisfy
    # this print's per-slot filament weight (#1496). Display-only flag — the
    # actual deficit is recomputed live every time the user clicks ▶, so
    # swapping a spool to a fuller one between flag and dispatch clears the
    # block automatically.
    filament_short: Mapped[bool] = mapped_column(Boolean, default=False)

    # User has acknowledged the filament-shortage warning for this item
    # ("Print Anyway"). Set by the start route when the user passes
    # skip_filament_check=true, or at queue-creation time if PrintModal's
    # frontend deficit warning was acknowledged. Survives scheduler ticks so
    # the dispatch no longer bounces between "user said anyway" and
    # "scheduler re-flagged" (#1698-followup).
    skip_filament_check: Mapped[bool] = mapped_column(Boolean, default=False)

    # Tracking
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # User tracking (who added this to the queue)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    printer: Mapped["Printer"] = relationship()
    archive: Mapped["PrintArchive | None"] = relationship()
    library_file: Mapped["LibraryFile | None"] = relationship()
    project: Mapped["Project | None"] = relationship(back_populates="queue_items")
    batch: Mapped["PrintBatch | None"] = relationship(back_populates="queue_items")
    created_by: Mapped["User | None"] = relationship()
    eject_profile: Mapped["EjectProfile | None"] = relationship()


from backend.app.models.archive import PrintArchive  # noqa: E402
from backend.app.models.eject_profile import EjectProfile  # noqa: E402
from backend.app.models.library import LibraryFile  # noqa: E402
from backend.app.models.print_batch import PrintBatch  # noqa: E402
from backend.app.models.printer import Printer  # noqa: E402
from backend.app.models.project import Project  # noqa: E402
from backend.app.models.user import User  # noqa: E402
