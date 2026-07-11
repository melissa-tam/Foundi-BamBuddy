"""Notification template model for customizable notification messages."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class NotificationTemplate(Base):
    """Model for notification message templates."""

    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    title_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


# Default templates for seeding
DEFAULT_TEMPLATES = [
    {
        "event_type": "print_start",
        "name": "Print Started",
        "title_template": "Print Started",
        "body_template": "{printer}: {filename}\nEstimated: {estimated_time}",
    },
    {
        "event_type": "print_complete",
        "name": "Print Completed",
        "title_template": "Print Completed",
        "body_template": "{printer}: {filename}\nTime: {duration}\nFilament: {filament_grams}g",
    },
    {
        "event_type": "print_failed",
        "name": "Print Failed",
        "title_template": "Print Failed",
        "body_template": "{printer}: {filename}\nTime: {duration}\nReason: {reason}",
    },
    {
        "event_type": "print_stopped",
        "name": "Print Stopped",
        "title_template": "Print Stopped",
        "body_template": "{printer}: {filename}\nTime: {duration}",
    },
    {
        "event_type": "print_progress",
        "name": "Print Progress",
        "title_template": "Print {progress}% Complete",
        "body_template": "{printer}: {filename}\nRemaining: {remaining_time}",
    },
    {
        "event_type": "print_missing_spool_assignment",
        "name": "Missing Spool Assignment",
        "title_template": "Missing Spool Assignment",
        "body_template": "{printer}: print started with missing spool assignments\nSlots: {missing_slots}\nExpected profile:\n{missing_slot_details}",
    },
    {
        "event_type": "printer_offline",
        "name": "Printer Offline",
        "title_template": "Printer Offline",
        "body_template": "{printer} has disconnected",
    },
    {
        "event_type": "printer_error",
        "name": "Printer Error",
        "title_template": "Printer Error: {error_type}",
        "body_template": "{printer}\n{error_detail}",
    },
    {
        "event_type": "ai_failure_detection",
        "name": "AI Failure Detection",
        "title_template": "Possible Print Failure Detected",
        "body_template": "{printer}: {task_name}\nConfidence: {confidence}\nAction taken: {action}",
    },
    {
        "event_type": "plate_not_empty",
        "name": "Plate Not Empty",
        "title_template": "Plate Not Empty — {printer}",
        # {source_detail} carries the source-specific sentence (printer vision /
        # camera comparison / cooldown timeout). Rendering is tolerant of an older
        # install whose seeded body lacks this placeholder — the detail is appended
        # (see NotificationService.on_plate_not_empty).
        "body_template": "{printer}: {source_detail}",
    },
    {
        "event_type": "filament_low",
        "name": "Filament Low",
        "title_template": "Filament Low",
        "body_template": "{printer}: Slot {slot} at {remaining_percent}%",
    },
    {
        "event_type": "maintenance_due",
        "name": "Maintenance Due",
        "title_template": "Maintenance Due",
        "body_template": "{printer}:\n{items}",
    },
    {
        "event_type": "ams_humidity_high",
        "name": "AMS Humidity High",
        "title_template": "AMS Humidity Alert",
        "body_template": "{printer} {ams_label}: Humidity {humidity}% exceeds {threshold}% threshold",
    },
    {
        "event_type": "ams_temperature_high",
        "name": "AMS Temperature High",
        "title_template": "AMS Temperature Alert",
        "body_template": "{printer} {ams_label}: Temperature {temperature}°C exceeds {threshold}°C threshold",
    },
    {
        "event_type": "bed_cooled",
        "name": "Bed Cooled",
        "title_template": "Bed Cooled",
        "body_template": "{printer}: Bed cooled to {bed_temp}°C (threshold: {threshold}°C)",
    },
    {
        "event_type": "first_layer_complete",
        "name": "First Layer Complete",
        "title_template": "First Layer Complete",
        "body_template": "{printer}: {filename}\nLayer 1/{total_layers} done",
    },
    {
        "event_type": "test",
        "name": "Test Notification",
        "title_template": "Bambuddy Test",
        "body_template": "This is a test notification. If you see this, notifications are working!",
    },
    # Queue notifications
    {
        "event_type": "queue_job_added",
        "name": "Queue Job Added",
        "title_template": "Job Queued",
        "body_template": "{job_name} added to queue for {target}",
    },
    {
        "event_type": "queue_job_assigned",
        "name": "Queue Job Assigned",
        "title_template": "Job Assigned",
        "body_template": "{job_name} assigned to {printer} (from Any {target_model} queue)",
    },
    {
        "event_type": "queue_job_started",
        "name": "Queue Job Started",
        "title_template": "Queue Job Started",
        "body_template": "{printer}: {job_name}\nEstimated: {estimated_time}",
    },
    {
        "event_type": "queue_job_waiting",
        "name": "Queue Job Waiting",
        "title_template": "Queue Job Waiting",
        "body_template": "{job_name} waiting for {target_model}\n{waiting_reason}",
    },
    {
        "event_type": "queue_job_skipped",
        "name": "Queue Job Skipped",
        "title_template": "Job Skipped",
        "body_template": "{printer}: {job_name}\nReason: {reason}",
    },
    {
        "event_type": "queue_job_failed",
        "name": "Queue Job Failed",
        "title_template": "Job Failed to Start",
        "body_template": "{printer}: {job_name}\nReason: {reason}",
    },
    {
        "event_type": "queue_completed",
        "name": "Queue Completed",
        "title_template": "Queue Complete",
        "body_template": "All {completed_count} queued jobs have finished",
    },
    # Farm production run notifications (Phase 3)
    {
        "event_type": "first_article_pending",
        "name": "First Article Pending Approval",
        "title_template": "First Article Ready — {sku_code}",
        "body_template": "{printer}: first article for run '{run_name}' printed and is awaiting approval.\nApprove or reject to continue the run.",
    },
    {
        "event_type": "printer_quarantined",
        "name": "Printer Quarantined",
        "title_template": "Printer Quarantined: {printer}",
        "body_template": "{printer} was quarantined after {failure_count} consecutive failures.\nReason: {reason}\nIt is excluded from dispatch until cleared.",
    },
    {
        "event_type": "run_paused",
        "name": "Production Run Paused",
        "title_template": "Run Paused — {sku_code}",
        "body_template": "Run '{run_name}' was paused.\nReason: {reason}",
    },
    {
        "event_type": "run_completed",
        "name": "Production Run Completed",
        "title_template": "Run Complete — {sku_code}",
        "body_template": "Run '{run_name}' finished: {units_completed} unit(s) across {plates_completed} plate(s).",
    },
    {
        "event_type": "foreign_job_detected",
        "name": "Foreign Print Detected",
        "title_template": "Foreign print detected: {printer}",
        "body_template": "{printer} finished '{subtask_name}' that Bambuddy did not dispatch.\nPlate gate raised; farm queue untouched. Clear the plate to resume dispatch.",
    },
    {
        "event_type": "model_mismatch",
        "name": "Printer Model Mismatch",
        "title_template": "Model mismatch: {printer}",
        "body_template": "{printer} reports model {reported_model} but is registered as {registered_model}.\nFarm dispatch is blocked — correct the printer's model so eject geometry matches the real bed.",
    },
    {
        "event_type": "run_unit_stopped",
        "name": "Run Unit Stopped",
        "title_template": "Unit stopped — {printer_name}",
        "body_template": "{printer_name}: unit stopped by the operator during run '{run_name}'. No auto-retry; clear the plate, then Resume the run to top it back up.",
    },
    {
        "event_type": "print_stalled",
        "name": "Print Stalled (printer offline)",
        "title_template": "Print stalled — {printer_name}",
        "body_template": "{printer_name} has been offline {minutes} min with '{job_name}' still marked printing. It will reconcile automatically when the printer reconnects.",
    },
    # Farm production run notifications (Phase 6: manual / lifecycle events)
    {
        "event_type": "run_aborted",
        "name": "Production Run Aborted",
        "title_template": "Run Aborted — {sku_code}",
        "body_template": "Run '{run_name}' was aborted by the operator.\nAll remaining plates were cancelled.",
    },
    {
        "event_type": "run_resumed",
        "name": "Production Run Resumed",
        "title_template": "Run Resumed — {sku_code}",
        "body_template": "Run '{run_name}' was resumed by the operator.\nTopped up {topped_up} plate(s) to plan.",
    },
    {
        "event_type": "first_article_approved",
        "name": "First Article Approved",
        "title_template": "First Article Approved — {sku_code}",
        "body_template": "First article for run '{run_name}' was approved on {printer}.\nRemaining plates released to dispatch.",
    },
    {
        "event_type": "user_created",
        "name": "Welcome Email",
        "title_template": "Welcome to {app_name}",
        "body_template": "Welcome {username}!\n\nYour account has been created.\nUsername: {username}\nPassword: {password}\n\nLogin at: {login_url}",
    },
    {
        "event_type": "password_reset",
        "name": "Password Reset",
        "title_template": "{app_name} - Password Reset",
        "body_template": "Hello {username},\n\nYour password has been reset.\nNew Password: {password}\n\nLogin at: {login_url}",
    },
    # Inventory stock alert templates
    {
        "event_type": "stock_reorder_alert",
        "name": "Stock Reorder Alert",
        "title_template": "Reorder Alert: {material}",
        "body_template": "{material} ({brand}) has reached the reorder point.\nStock: {stock_g}g | Rate: {rate_g_day}g/day | Days left: {days_left}d\nReorder now to avoid a stock break.",
    },
    {
        "event_type": "stock_break_alert",
        "name": "Stock Break Alert",
        "title_template": "Stock Break Risk: {material}",
        "body_template": "{material} ({brand}) will run out before replenishment arrives.\nStock: {stock_g}g | Rate: {rate_g_day}g/day | Lead time: {lead_time_days}d\nOnly {days_left}d of stock remaining — order immediately.",
    },
    # User email notification templates (sent to the print job owner).
    # Names include " Email" so they aren't confused with the provider-level
    # `print_*` templates above, which share the same body shape but are
    # broadcast to admin-configured providers (ntfy/pushover/telegram/discord/
    # etc.) rather than mailed to a specific user.
    {
        "event_type": "user_print_start",
        "name": "User Print Started Email",
        "title_template": "Your Print Has Started",
        "body_template": "Hello {username},\n\nYour print job has started on {printer}.\n\nFile: {filename}\n\nYou will be notified when it completes.",
    },
    {
        "event_type": "user_print_complete",
        "name": "User Print Completed Email",
        "title_template": "Your Print Is Complete",
        "body_template": "Hello {username},\n\nYour print job has completed on {printer}.\n\nFile: {filename}",
    },
    {
        "event_type": "user_print_failed",
        "name": "User Print Failed Email",
        "title_template": "Your Print Has Failed",
        "body_template": "Hello {username},\n\nYour print job has failed on {printer}.\n\nFile: {filename}",
    },
    {
        "event_type": "user_print_stopped",
        "name": "User Print Stopped Email",
        "title_template": "Your Print Has Been Stopped",
        "body_template": "Hello {username},\n\nYour print job was stopped on {printer}.\n\nFile: {filename}",
    },
]
