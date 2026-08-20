from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.ext.hybrid import hybrid_property
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
    # The roll's SECOND RFID chip, once the AMS has read it. A Bambu roll physically
    # carries exactly TWO tags — one per flange side — sharing one ``tray_uuid``, and the
    # AMS reads whichever side faces its antenna, so the wire ``tag_uid`` for one roll
    # legitimately alternates between two values (live-proven 4/4 fleet slots 2026-08-01).
    #
    # 3NF note: this is a COLUMN, not a child table, because the hardware cardinality is
    # a fixed 2 — not "zero or more tags per roll" but "exactly two chips on one physical
    # object", the same way a person's row carries first and last name rather than a
    # names table. A tag_uid/sibling_tag_uid pair is a single-valued fact about the roll
    # (which two chips it carries), fully dependent on the spool key and on nothing else.
    #
    # NULL = only one side has ever been read (the normal state — a roll seated the same
    # way up every time never shows its other chip). Written ONCE, on first sighting, by
    # the slot pipeline's sibling-read path; a THIRD distinct tag is physically impossible
    # for a genuine roll and is refused + WARNed rather than overwriting the pair.
    # Compare through ``utils.tag_normalization.tag_matches_row`` — never against
    # ``tag_uid`` alone.
    sibling_tag_uid: Mapped[str | None] = mapped_column(String(32))
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
    # Durable "a tagless fresh-roll prompt (W5) is awaiting an operator answer for
    # this row" stamp — the per-CYCLE prompt's only state. Process memory could not
    # hold it: a broadcast with no client connected reached nobody, a restart wiped
    # the set, and the reconnect replay then had nothing to replay (2026-07-24).
    # Re-stamped (not deduped away) on every new qualified physical cycle — that IS
    # the per-cycle re-ask — and NULLed by either answer (fresh / same).
    fresh_prompt_pending_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Out-of-rotation marker: set when a feed-fault HMS (stuck/tangled spool)
    # triggers mid-print recovery on this spool's tray; NULL = in rotation.
    # Cleared on physical remove+re-insert (ams_presence edge) or manual PATCH.
    # Distinct from spent_at (hardware exhaustion) and archived_at (soft-hide):
    # a jammed spool is neither.
    feed_fault_at: Mapped[datetime | None] = mapped_column(DateTime)
    # The HMS short code (e.g. "0700_8010") that flagged the feed fault.
    feed_fault_code: Mapped[str | None] = mapped_column(String(16))
    # FIFO substrate: when this spool FIRST entered service (first time it got a
    # SpoolAssignment). WRITE-ONCE ledger history
    # (``spool_binding.stamp_first_loaded`` only writes when NULL) — do NOT consume
    # this as the physical seating order. NULL = never loaded (a pristine,
    # never-assigned inventory spool).
    first_loaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    # FIFO ordinal: when the physical roll CURRENTLY in the tray entered service —
    # the re-stampable seating order the ``first_loaded`` selection policy sorts by.
    # Re-stamped (``spool_binding.stamp_loaded`` / ``.stamp_loaded_for_slot``) on a binding change
    # to a different spool row and on a never-fed row's re-seat; a mid-life or
    # RFID-same-tag re-seat keeps its value (grams-state + identity adjudicated, no
    # timing input). Distinct from ``first_loaded_at`` (write-once history), which
    # 006-H2S proved was wrongly overloaded as seating order (a stale ledger row
    # lent its age to a fresh roll). NULL falls back to ``first_loaded_at`` /
    # ``created_at`` in the selector.
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Last AMS slot this roll was RELEASED from, stamped by the one unbind writer
    # (``spool_binding.release_spool_from_slot`` and the move/displacement sweep in
    # ``bind_spool_to_slot``). NULL = never released from a slot.
    #
    # DOCUMENTED DENORMALIZATION (3NF exception, 2026-08-01): the identical fact is
    # already in the ``[slot-state] … release`` log line the same writer emits, and
    # that line is the normal-form source of record. It is duplicated onto the row
    # because (a) logs are retention-rotated while a roll can sit on a drying shelf
    # for weeks, and (b) the reclaim lane must find "the roll that last sat in THIS
    # slot" as a plain indexed column read on candidate spools, not by parsing a log
    # stream. Written ONLY by ``spool_binding`` — one writer, so it cannot drift.
    #
    # ``last_location_printer_id`` deliberately carries NO ForeignKey: it is a
    # historical observation, not a live reference, and a deleted printer must not
    # cascade away a roll's gram-continuity hint.
    last_location_printer_id: Mapped[int | None] = mapped_column(Integer)
    last_location_ams_id: Mapped[int | None] = mapped_column(Integer)
    last_location_tray_id: Mapped[int | None] = mapped_column(Integer)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    k_profiles: Mapped[list["SpoolKProfile"]] = relationship(back_populates="spool", cascade="all, delete-orphan")
    assignments: Mapped[list["SpoolAssignment"]] = relationship(back_populates="spool", cascade="all, delete-orphan")
    location: Mapped["Location | None"] = relationship(back_populates="spools")

    @hybrid_property
    def is_finished_roll(self) -> bool:
        """THE one encoding of "a spent row is a FINISHED roll" (doctrine rules 3 and 8).

        A runout means this row reached ZERO. You cannot add filament to a 0 g roll, so
        whatever is physically in the slot afterwards is a DIFFERENT roll — on a reused
        core if the same RFID tag comes back (operator ruling 3, 2026-08-19). That single
        statement licenses two OPPOSITE conclusions, which is precisely why it is named
        once here instead of being spelled out at each site:

        * **The untagged lane EXCLUDES it** — ``spool_tag_matcher.find_matching_untagged_spool``
          filters ``~Spool.is_finished_roll``: a newly-arriving tag must always land on a
          DIFFERENT row, because claiming this one would hand a fresh roll a spent latch
          (hard-excluded from selection, and no automatic un-spend lane exists to undo it).
        * **The tagged lane READS it as evidence** — ``spool_respool``'s tier 2 concludes
          from ``spool.is_finished_roll`` plus a LOADED tray that the tag came back on a
          reused core, and re-spools it onto a fresh row. Concluded from evidence, never
          asked (scenario G3).

        A ``hybrid_property`` because those two call sites live on opposite sides of the
        ORM boundary — one is a WHERE clause, the other a row already in hand — and a
        second spelling is exactly how the doctrine drifts: the same two-copies shape the
        2026-08-19 wave deleted from ``_is_tagless``, where a later identity-column change
        updated one copy and not the other. Derived at read time from ``spent_at``; there
        is NO stored flag, for the same reason :attr:`remaining_g` has none (rule 8 — the
        ledger stays raw, so clearing ``spent_at`` alone un-finishes the row).
        """
        return self.spent_at is not None

    @is_finished_roll.inplace.expression
    @classmethod
    def _is_finished_roll_expression(cls):
        """SQL form of :attr:`is_finished_roll` — same statement, WHERE-clause side."""
        return cls.spent_at.is_not(None)

    @property
    def remaining_g(self) -> float:
        """THE one derivation of a row's remaining grams — emptiness is derived from
        ``spent_at`` (doctrine rule 8), never written into the gram ledger.

        A spent row reads 0.0 whatever the ledger says (an under-counted
        ``weight_used`` must not present a run-dry roll as printable material), while
        ``weight_used`` itself stays raw and lossless on the row — the operator-gated
        un-spend restores the true remaining figure by clearing ``spent_at`` alone.
        """
        if self.spent_at is not None:
            return 0.0
        return max(0.0, float(self.label_weight or 0.0) - float(self.weight_used or 0.0))

    @property
    def delivered_g(self) -> float | None:
        """What this roll ACTUALLY held, once the hardware has said it is empty.

        ``label_weight`` on a minted row is an ASSUMPTION — the tagless default's nominal
        figure, not a reading of the roll in the tray. The hardware runout is the only
        moment the truth becomes knowable, and at that moment the answer is simply the
        grams the farm watched it feed. So this is a DERIVATION, never a column and never a
        write-back over ``label_weight``: overwriting the label would conflate "what we
        assumed" with "what it delivered" in one field and destroy the nominal figure that
        cost-per-kg and inventory reporting read (operator ruling 18).

        ``None`` until ``spent_at`` is stamped — an un-spent roll has delivered nothing
        final, and a caller must not treat its running total as a capacity.

        The gap between assumption and delivery is NORMAL in both directions and is not an
        error to be flagged: a part-used roll an operator seated delivers ~800 g on a
        1000 g label, and some brands ship ~1100 g on that same label. Both are ordinary
        stock, which is why the spent stamp records this figure instead of warning about it.
        """
        if self.spent_at is None:
            return None
        return max(0.0, float(self.weight_used or 0.0))


from backend.app.models.location import Location  # noqa: E402
from backend.app.models.spool_assignment import SpoolAssignment  # noqa: E402
from backend.app.models.spool_k_profile import SpoolKProfile  # noqa: E402
