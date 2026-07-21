"""Notification provider and log models for push notifications."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from backend.app.core.database import Base


class NotificationDigestQueue(Base):
    """Model for queuing notifications to be sent in daily digest."""

    __tablename__ = "notification_digest_queue"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("notification_providers.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(50), nullable=False)  # print_start, print_complete, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    printer_name = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    provider = relationship("NotificationProvider", back_populates="digest_queue")


class NotificationLog(Base):
    """Model for logging sent notifications."""

    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("notification_providers.id", ondelete="CASCADE"), nullable=False)
    event_type = Column(String(50), nullable=False)  # print_start, print_complete, etc.
    title = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)
    success = Column(Boolean, default=True)
    error_message = Column(Text, nullable=True)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)
    printer_name = Column(String(100), nullable=True)  # Store name in case printer is deleted
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # Relationships
    provider = relationship("NotificationProvider", back_populates="logs")


class NotificationProvider(Base):
    """Model for notification providers (WhatsApp, ntfy, Pushover, etc.)."""

    __tablename__ = "notification_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # User-defined name
    provider_type = Column(String(50), nullable=False)  # callmebot, ntfy, pushover, telegram, email
    enabled = Column(Boolean, default=True)

    # Provider-specific configuration stored as JSON string
    config = Column(Text, nullable=False)

    # Event triggers - print lifecycle
    on_print_start = Column(Boolean, default=False)
    on_print_complete = Column(Boolean, default=True)
    on_print_failed = Column(Boolean, default=True)
    on_print_stopped = Column(Boolean, default=True)  # User cancelled/stopped print
    on_print_progress = Column(Boolean, default=False)  # 25%, 50%, 75% milestones
    on_print_missing_spool_assignment = Column(Boolean, default=False)  # Print started with unassigned required tray(s)

    # Event triggers - printer status
    on_printer_offline = Column(Boolean, default=False)
    on_printer_error = Column(Boolean, default=False)  # AMS issues, etc.
    on_ai_failure_detection = Column(Boolean, default=False)  # Obico spaghetti / failure detection (#1794)
    on_filament_low = Column(Boolean, default=False)
    on_maintenance_due = Column(Boolean, default=False)  # Maintenance reminder

    # Event triggers - AMS environmental alarms (regular AMS with 4 slots)
    on_ams_humidity_high = Column(Boolean, default=False)  # AMS humidity above threshold
    on_ams_temperature_high = Column(Boolean, default=False)  # AMS temperature above threshold

    # Event triggers - AMS-HT environmental alarms (single slot heated AMS)
    on_ams_ht_humidity_high = Column(Boolean, default=False)  # AMS-HT humidity above threshold
    on_ams_ht_temperature_high = Column(Boolean, default=False)  # AMS-HT temperature above threshold

    # Event triggers - Build plate detection
    on_plate_not_empty = Column(Boolean, default=True)  # Objects detected on plate before print

    # Event triggers - Bed cooled after print
    on_bed_cooled = Column(Boolean, default=False)  # Bed cooled below threshold after print
    on_first_layer_complete = Column(Boolean, default=False)  # First layer finished printing

    # Event triggers - Inventory stock alerts
    on_stock_reorder_alert = Column(Boolean, default=False)  # SKU hits reorder point
    on_stock_break_alert = Column(Boolean, default=False)  # Stock will run out before replenishment

    # Event triggers - Print queue
    on_queue_job_added = Column(Boolean, default=False)  # Job added to queue
    on_queue_job_assigned = Column(Boolean, default=False)  # Model-based job assigned to printer
    on_queue_job_started = Column(Boolean, default=False)  # Queue job started printing
    on_queue_job_waiting = Column(Boolean, default=True)  # Job waiting for filament or printer
    on_queue_job_skipped = Column(Boolean, default=True)  # Job skipped (previous print failed)
    on_queue_job_failed = Column(Boolean, default=True)  # Job failed to start
    on_queue_completed = Column(Boolean, default=False)  # All pending jobs finished

    # Event triggers - Farm production (Phase 3)
    on_first_article_pending = Column(Boolean, default=True)  # First article printed, awaiting approval
    on_printer_quarantined = Column(Boolean, default=True)  # Printer quarantined after consecutive failures
    on_run_paused = Column(Boolean, default=True)  # Production run paused (reject / no printers)
    on_run_completed = Column(Boolean, default=False)  # Production run finished all plates
    on_foreign_job_detected = Column(Boolean, default=True)  # Terminal print Bambuddy did not dispatch (foreign)
    on_model_mismatch = Column(Boolean, default=True)  # Device-reported model differs from registered Printer.model
    on_run_unit_stopped = Column(Boolean, default=True)  # Run unit stopped by the operator (UI or printer screen)
    on_print_stalled = Column(Boolean, default=True)  # Printing unit's printer offline past the stall grace window
    on_print_paused_stalled = Column(
        Boolean, default=True
    )  # Printing unit's CONNECTED printer sat unattended-PAUSEd past the pause-stall grace window
    on_storage_low = Column(
        Boolean, default=True
    )  # Printer USB storage FAILURE — auto-cleanup could not free space, FTPS/USB unreachable, or the drive dropped mid-print (successful cleanups are silent)
    on_spool_recovery_succeeded = Column(Boolean, default=True)  # farm: mid-print jam auto-recovered
    on_spool_recovery_failed = Column(Boolean, default=True)  # farm: jam recovery escalated, printer left paused
    on_spool_out_of_rotation = Column(Boolean, default=True)  # farm: spool flagged out of rotation after feed fault
    on_spool_recovery_self_healed = Column(
        Boolean, default=True
    )  # farm: feed fault cleared by a firmware retry on the SAME spool (no swap, no out-of-rotation)
    on_cooldown_escalation = Column(
        Boolean, default=True
    )  # Post-print eject cooldown running long (bed still above threshold past the escalation window)

    # Event triggers - Farm production (Phase 6: manual/lifecycle events)
    on_run_aborted = Column(Boolean, default=True)  # Production run aborted by the operator (destructive)
    on_run_resumed = Column(Boolean, default=False)  # Paused production run resumed by the operator (informational)
    on_first_article_approved = Column(Boolean, default=True)  # First article approved (physical or remote eject)

    # Quiet hours (do not disturb)
    quiet_hours_enabled = Column(Boolean, default=False)
    quiet_hours_start = Column(String(5), nullable=True)  # HH:MM format, e.g., "22:00"
    quiet_hours_end = Column(String(5), nullable=True)  # HH:MM format, e.g., "07:00"

    # Daily digest (batch notifications into a single daily summary)
    daily_digest_enabled = Column(Boolean, default=False)
    daily_digest_time = Column(String(5), nullable=True)  # HH:MM format, e.g., "08:00"

    # Optional: Link to specific printer (NULL = all printers)
    printer_id = Column(Integer, ForeignKey("printers.id", ondelete="SET NULL"), nullable=True)

    # Status tracking
    last_success = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    printer = relationship("Printer", back_populates="notification_providers")
    logs = relationship("NotificationLog", back_populates="provider", cascade="all, delete-orphan")
    digest_queue = relationship("NotificationDigestQueue", back_populates="provider", cascade="all, delete-orphan")
