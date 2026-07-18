from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.database import Base


class Spool(Base):
    """Spool inventory item for tracking filament spools and their properties."""

    __tablename__ = "spool"

    id: Mapped[int] = mapped_column(primary_key=True)
    material: Mapped[str] = mapped_column(String(50))  # PLA, PETG, ABS, etc.
    subtype: Mapped[str | None] = mapped_column(String(50))  # Basic, Matte, Silk, etc.
    color_name: Mapped[str | None] = mapped_column(String(100))  # "Jade White"
    rgba: Mapped[str | None] = mapped_column(String(8))  # RRGGBBAA hex
    # Multi-colour gradient stops for filaments with more than one colour
    # (e.g. tri-colour, multi-colour). Stored as comma-separated 6- or 8-char
    # hex tokens without `#`. Empty/NULL means solid (uses `rgba`). Up to 8
    # stops; combination mode is driven by `subtype` (Gradient, Multicolor).
    extra_colors: Mapped[str | None] = mapped_column(String(255))
    # Visual effect overlay independent of subtype: sparkle, wood, marble,
    # glow, matte. Purely a rendering hint — does not affect MQTT/firmware.
    effect_type: Mapped[str | None] = mapped_column(String(20))
    brand: Mapped[str | None] = mapped_column(String(100))  # "Polymaker"
    label_weight: Mapped[int] = mapped_column(Integer, default=1000)  # Advertised net weight (g)
    core_weight: Mapped[int] = mapped_column(Integer, default=250)  # Empty spool weight (g)
    core_weight_catalog_id: Mapped[int | None] = mapped_column(
        Integer
    )  # Reference to spool_catalog entry for core weight
    weight_used: Mapped[float] = mapped_column(Float, default=0)  # Consumed grams
    # Anchor for the resettable "Total Consumed" stat. The displayed counter
    # is `weight_used - weight_used_baseline`; the Inventory page's "Reset
    # usage to 0" action stamps baseline = weight_used so the counter zeroes
    # without disturbing remaining (= label_weight - weight_used). Matches
    # Spoolman's split between used_weight and remaining_weight (#1390).
    weight_used_baseline: Mapped[float] = mapped_column(Float, default=0)
    weight_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # Lock weight from AMS auto-sync
    last_scale_weight: Mapped[int | None] = mapped_column(Integer)  # Last gross weight from scale (g)
    last_weighed_at: Mapped[datetime | None] = mapped_column(DateTime)  # When last weighed
    slicer_filament: Mapped[str | None] = mapped_column(String(50))  # Preset ID (e.g. "GFL99")
    slicer_filament_name: Mapped[str | None] = mapped_column(String(100))  # Preset name for slicer
    nozzle_temp_min: Mapped[int | None] = mapped_column()  # Override min temp
    nozzle_temp_max: Mapped[int | None] = mapped_column()  # Override max temp
    note: Mapped[str | None] = mapped_column(String(500))
    added_full: Mapped[bool | None] = mapped_column()  # Whether spool was added as full (unused)

    # User-defined category (e.g. "Production", "Prototype", "Client A") for
    # filtering and per-group low-stock thresholds (#729). Free text — the
    # form autocompletes from categories already present on other spools.
    category: Mapped[str | None] = mapped_column(String(50))
    # Per-spool override of the global inventory low-stock threshold (%).
    # NULL falls back to the `low_stock_threshold` setting. Lets users mark
    # production spools with a higher threshold (alert earlier) and prototype
    # spools with a lower one without changing the global default.
    low_stock_threshold_pct: Mapped[int | None] = mapped_column(Integer)

    # Cost tracking
    cost_per_kg: Mapped[float | None] = mapped_column(Float)  # Cost per kilogram

    storage_location: Mapped[str | None] = mapped_column(String(255))  # User-editable storage location
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), index=True)

    last_used: Mapped[datetime | None] = mapped_column(DateTime)  # Last time this spool was used in a print
    encode_time: Mapped[datetime | None] = mapped_column(DateTime)  # When spool was encoded/written to tag
    tag_uid: Mapped[str | None] = mapped_column(String(32))  # RFID tag UID (up to 32 hex chars)
    tray_uuid: Mapped[str | None] = mapped_column(String(32))  # Bambu Lab spool UUID (32 hex chars)
    data_origin: Mapped[str | None] = mapped_column(String(20))  # How data was populated: manual, rfid_auto, nfc_link
    tag_type: Mapped[str | None] = mapped_column(String(20))  # Tag vendor: bambulab, generic, bambulab_reused, etc.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)  # NULL = active
    # Hardware-observed exhaustion marker for the reused-tag auto re-spool flow.
    # Set ONLY when the AMS physically saw the filament end (runout HMS / seamless
    # backup-swap) — NEVER by gram estimates or the AMS remain%. It is the
    # certainty key that gates the automatic re-spool tier; NULL = never observed
    # spent (falls back to the one-click prompt tier).
    spent_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Operator answered "Same spool" to the tier-3 (uncertain) re-spool prompt:
    # suppresses further tier-3 prompts for this spool across reseats / AMS
    # power-cycles / server restarts (the in-memory prompt dedup cannot survive
    # those). Hardware-certain spent (tier 1/2 auto re-spool) is NOT gated by
    # this — only the uncertain prompt tier reads it, so a genuine exhaustion
    # still surfaces. Stamped ONLY via POST /inventory/spools/{id}/respool-dismiss
    # (the single mutator — deliberately absent from SpoolUpdate).
    respool_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Out-of-rotation marker: set when a feed-fault HMS (stuck/tangled spool)
    # triggers mid-print recovery on this spool's tray; NULL = in rotation.
    # Cleared on physical remove+re-insert (ams_presence edge) or manual PATCH.
    # Distinct from spent_at (hardware exhaustion) and archived_at (soft-hide):
    # a jammed spool is neither.
    feed_fault_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The HMS short code (e.g. "0700_8010") that flagged the feed fault.
    feed_fault_code: Mapped[str | None] = mapped_column(String(16))
    # FIFO substrate: when this spool FIRST entered service (first time it got a
    # SpoolAssignment). Stamped by later work items — the column exists so the
    # spool-selection policy can order candidates oldest-first. NULL = never
    # loaded (a pristine, never-assigned inventory spool).
    first_loaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    k_profiles: Mapped[list["SpoolKProfile"]] = relationship(back_populates="spool", cascade="all, delete-orphan")
    assignments: Mapped[list["SpoolAssignment"]] = relationship(back_populates="spool", cascade="all, delete-orphan")
    location: Mapped["Location | None"] = relationship(back_populates="spools")


from backend.app.models.location import Location  # noqa: E402
from backend.app.models.spool_assignment import SpoolAssignment  # noqa: E402
from backend.app.models.spool_k_profile import SpoolKProfile  # noqa: E402
