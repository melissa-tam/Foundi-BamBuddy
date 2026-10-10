from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: The CLOSED set of answers "Re-check slot" can give — doctrine rule 12's contract table
#: (``bambu-ams-behavior`` ``resources/spool-subsystem.md`` §4.1, rows R1–R8) spelled as a
#: type, so neither the service that decides nor the wire that carries can invent a sixth.
#:
#: Lives here, in the dependency-free DTO layer, because both sides need it at RUNTIME:
#: pydantic validates against it and ``slot_recheck.RecheckVerdict`` is typed by it. The
#: reverse direction — the service owning it and the schema importing it — would pull the
#: slot pipeline's whole model graph into every import of this module.
RecheckOutcome = Literal["unchanged", "minted", "identified", "queued", "empty", "restored"]

#: The CLOSED set of answers an operator's AMS Load / Unload click can get —
#: ``services/ams_command.command_for_operator`` returns exactly one, and
#: ``api/routes/printers.py`` only maps it (the two refusals to 400 / 409, every wire
#: answer to 200). The six wire answers are ``ams_command.Answer``, measured by the ONE
#: classifier (``held`` = the firmware acknowledged the command and nothing moved: it
#: sits behind the paused print's filament change); the two refusals are the only
#: pre-publish refusals kept (no client, or a failed publish; a standing runout hold).
#: Lives here, in the dependency-free DTO layer, for ``RecheckOutcome``'s reason: the
#: service is typed by it and the wire validates it.
AmsCommandOutcome = Literal[
    "refused_not_connected",
    "refused_runout_hold",
    "complete",
    "acted",
    "no_movement",
    "held",
    "undecidable",
    "session_changed",
]

#: The posture FAMILY an operator's AMS command was sent into (``ams_command.PostureFamily``,
#: pinned equal by ``test_printers_api``): ``mid_change`` — inside the paused print's own filament
#: change, where a command the firmware acknowledges is held behind it; ``outside_change`` — every
#: other posture. A client keys its posture-specific copy off it; ``None`` for a refusal.
AmsPostureFamily = Literal["mid_change", "outside_change"]

#: An AMS MOTION command, as a client names it (``ams_command.Command`` — the only publisher of the
#: ledger's command steps; pinned equal by ``test_printer_manager``): the motion the "Toolhead
#: empty" chip speaks of (:class:`ToolheadRefillState`).
AmsMotionCommand = Literal["load", "unload"]

#: What a Resume would do about an EMPTY toolhead — K7's closed reason (``refill_verdict.
#: RefillReason``, pinned equal by ``test_printer_manager``): ``owed`` — the farm refills it, then
#: resumes; the firmware feeds it itself (``before_first_layer`` — the start block's own load;
#: ``change_in_flight``, ``runout_demand``, ``last_layer``, ``eject_sweep``, ``power_loss_prompt``);
#: refused (``maintenance``, ``command_pending``, ``physical``, ``unknown``); ``fed`` never reaches a
#: client (the field rides an empty feed only). :class:`ToolheadState`.
ToolheadRefillReason = Literal[
    "owed",
    "fed",
    "maintenance",
    "physical",
    "runout_demand",
    "power_loss_prompt",
    "before_first_layer",
    "last_layer",
    "change_in_flight",
    "command_pending",
    "eject_sweep",
    "unknown",
]

#: The CLOSED set of reasons the resume of a paused print is REFUSED (K9, 2026-10-10 —
#: ``spool_recovery.resume_paused_print``; the route maps each to a 409). The verb's own two —
#: ``not_paused`` and ``farm_acting`` (a recovery driver is live and owns the printer's next
#: motion) — then the refill verdict's reasons whose resume would print air: ``maintenance`` (the
#: farm loads nothing in maintenance mode), ``unknown`` (no live reading of the toolhead),
#: ``command_pending`` (the AMS still holds the farm's queued command) and ``physical``. Here, in
#: the dependency-free DTO layer, for ``RecheckOutcome``'s reason: the service is typed by it and
#: the wire validates it.
ResumeRefusalReason = Literal["not_paused", "farm_acting", "maintenance", "unknown", "command_pending", "physical"]

#: The CLOSED set of answers a manual eject can give. ``eject/manual.manual_eject``
#: returns exactly one :class:`~backend.app.services.eject.manual.EjectVerdict` carrying
#: one of these, and ``api/routes/printer_eject.py`` maps each to its HTTP shape — the
#: 2026-08-20 ``slot_recheck`` precedent, applied to the lane whose control flow used to
#: be three exception classes raised from four nesting levels.
#:
#: ``needs_input`` is the one that earns the type: it is NOT an error. It is the eject
#: asking the operator for the one fact only they have (the part height) and the one
#: choice only they can make (the sweep profile), and it reaches the wire as the same
#: ``409 {"code": "foreign_plate"}`` the dialog has always opened on.
EjectOutcome = Literal["dispatched", "released_watch", "needs_input", "bed_hot", "refused"]

#: Who put the part on the plate, as far as the farm can tell. Decides the dialog's
#: title and sentence only — the flow is identical for all three.
EjectOrigin = Literal["foreign", "farm_unit", "declared"]

#: The CLOSED set of reasons an eject is REFUSED — a state the operator's input cannot
#: cure (anything it CAN cure is ``needs_input`` instead). Reaches the wire verbatim as
#: the error ``code``, so the frontend maps codes to i18n keys and no English crosses
#: the service boundary.
#:
#: The first four are the occupancy authority's own
#: :data:`~backend.app.services.plate_occupancy.TransitionRefusal` tokens, kept
#: spelling-identical on purpose: one refusal vocabulary from the state machine to the
#: dialog. ``no_plate_gate`` is the authority's ``not_occupied`` under the name the API
#: has always used for it, and survives only for declare-less callers — every UI surface
#: declares occupancy, so it can no longer be reached from a printer card.
#: ``z_unreferenced`` (2026-09-04) is the fifth authority token: the printer rebooted
#: with a part on the plate, so its Z frame is fiction and no sweep may run against it
#: until a human removes the part and marks the plate cleared.
EjectRefusalReason = Literal[
    "job_active",
    "dispatch_in_flight",
    "eject_in_flight",
    "z_unreferenced",
    "not_connected",
    "no_plate_gate",
    "bed_unreadable",
    "first_article",
    "no_donor",
    "not_found",
    "profile_not_found",
]


class PrinterBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    serial_number: str = Field(..., min_length=1, max_length=50)

    @field_validator("serial_number")
    @classmethod
    def _normalize_serial_number(cls, v: str) -> str:
        """Uppercase and trim the serial number.

        Bambu serial numbers are uppercase alphanumeric, and the MQTT report
        topic ``device/<serial>/report`` is case-sensitive. A serial entered
        in the wrong case (or with stray whitespace) connects and subscribes
        without error but never receives a message — the printer publishes to
        the correctly-cased topic, so every status field stays unknown (#1465).
        Normalising on input makes the subscribed topic always match.
        """
        normalized = v.strip().upper()
        if not normalized:
            raise ValueError("serial_number must not be blank")
        return normalized

    ip_address: str = Field(
        ...,
        max_length=253,
        pattern=r"^(\d{1,3}(\.\d{1,3}){3}|[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)$",
    )
    model: str | None = None
    location: str | None = None  # Group/location name
    auto_archive: bool = True
    external_camera_url: str | None = None
    external_camera_type: str | None = None  # "mjpeg", "rtsp", "snapshot", "usb"
    external_camera_enabled: bool = False
    external_camera_snapshot_url: str | None = None  # Optional single-frame override; #1177
    camera_rotation: int = 0  # 0, 90, 180, 270 degrees


class PrinterCreate(PrinterBase):
    # access_code lives on the input shapes only — never on the default
    # PrinterResponse. Direct exposure on PRINTERS_READ would let a Viewer
    # connect to the printer's MQTT and bypass Bambuddy's RBAC.
    access_code: str = Field(..., min_length=1, max_length=20)


class PlateDetectionROI(BaseModel):
    """Region of interest for plate detection (percentages 0.0-1.0)."""

    x: float = Field(..., ge=0.0, le=1.0)  # X start %
    y: float = Field(..., ge=0.0, le=1.0)  # Y start %
    w: float = Field(..., ge=0.0, le=1.0)  # Width %
    h: float = Field(..., ge=0.0, le=1.0)  # Height %


class PrinterUpdate(BaseModel):
    name: str | None = None
    ip_address: str | None = Field(
        default=None,
        max_length=253,
        pattern=r"^(\d{1,3}(\.\d{1,3}){3}|[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*)$",
    )
    access_code: str | None = None
    model: str | None = None
    location: str | None = None
    is_active: bool | None = None
    auto_archive: bool | None = None
    print_hours_offset: float | None = None
    external_camera_url: str | None = None
    external_camera_type: str | None = None
    external_camera_enabled: bool | None = None
    external_camera_snapshot_url: str | None = None  # #1177
    camera_rotation: int | None = None  # 0, 90, 180, 270 degrees
    plate_detection_enabled: bool | None = None
    plate_detection_roi: PlateDetectionROI | None = None


class ServiceHoldState(BaseModel):
    """Maintenance mode: a human has this printer, and every automatic lane stands down.

    Present ⇔ the printer carries an OPEN ``service_hold`` incident; ``None`` means it
    does not. Deliberately a nested object rather than a bare timestamp, so the field
    reads as a STATE the UI branches on (the hold banner) instead of an optional number,
    and so a later fact about the hold extends the object rather than the printer payload.

    ``since`` is the incident's ``created_at`` as an ISO string — a JSON PRIMITIVE, not a
    ``datetime``: the same payload rides ``printer_state_to_dict`` through the WebSocket
    serializer's bare ``json.dumps``, which has no encoder behind it.

    It is NOT ``is_active``. That flag means "this instance holds an MQTT session"
    (Deactivated in the UI); a service hold keeps the session up on purpose — status
    visible, manual verbs working — and takes the printer out of the automatic lanes.

    ``since`` is nullable for one reason only: the OBJECT is the hold, so a row with no
    readable timestamp must still project as held rather than vanish into ``None``.
    """

    since: str | None = None


class PrinterMessageInfo(BaseModel):
    """One HMS message the printer showed, as RECORDED by the farm.

    Rendered by the backend's one catalog renderer (``hms_errors.PrinterMessage``), so
    the frontend holds no code→text table. ``description`` is ``""`` when no catalog
    knows the code.
    """

    short_code: str
    description: str = ""


class PlateCheckStopInfo(BaseModel):
    """A plate-check episode's LAST farm stop, as its ledger recorded it: the stop's name is the
    retry-or-escalate decision the ladder made once (``retry_stop`` / ``deadline_stop``;
    ``terminal_outcome``), ``outcome`` what the read of it answered (``None`` while unanswered)."""

    name: str
    outcome: str | None = None


class PlateCheckExit(BaseModel):
    """The plate-check HUMAN'S TURN (operator ruling 2026-10-05): the printer waits PAUSED at its
    plate-check dialog for a person — Ignore and resume, Problem solved, or Stop.

    Present ⇔ ``printer_incidents.plate_check_human_turn`` holds (an escalated ``plate_vision``
    row, no farm driver live, PAUSEd at the plate check on the row's job); ``None`` otherwise —
    including while the farm's own ladder is acting. Built by
    ``printer_manager.plate_check_exit_payload`` for BOTH ``/status`` branches and the WS frame.
    Its OWN field rather than part of ``open_incident``, because that chip shows the
    highest-precedence row and an AMS or power-loss row outranks ``plate_vision``.

    ``print_error`` and ``job_id`` are what ``POST /printers/{id}/hms/execute-action`` takes for
    the press; ``actions`` the buttons the turn offers (``PROBLEM_SOLVED_RESUME``,
    ``IGNORE_RESUME``); ``deadline_at`` when the farm stops the print if nobody presses (ISO,
    naive UTC like ``created_at``; ``None`` for the FALLBACK hold, which has no deadline, and
    until the hold's timer has computed it). JSON primitives only (the WS frame dumps bare).
    """

    print_error: str
    job_id: str = ""
    actions: list[str] = []
    deadline_at: str | None = None


class OpenIncidentState(BaseModel):
    """The printer's highest-precedence OPEN equipment-fault row, as the card reads it.

    Present ⇔ the printer carries an open incident; ``None`` means it does not. Built
    by ``printer_manager.open_incident_payload`` for BOTH ``/status`` branches and the
    WS frame, so the chip cannot appear on the socket push and vanish on the next poll.

    ``operator_exits`` is the load-bearing field and the reason this schema exists: it
    answers "would **Recover** end this hold" (``printer_incidents.closed_by_recover``),
    so the card can offer the verb from the RULE instead of guessing from a plate gate
    or a quarantine it happens to see. 011-H2S 2026-09-17 had neither, so the only verb
    that could have closed its hold was never rendered — and the same unreachable shape
    already existed for a ``z_reference_lost`` hold. The class vocabulary deliberately
    stays off the wire: a UI branching on ``"repair"`` would own a copy of the rule.

    Only JSON PRIMITIVES (``created_at`` is an ISO string, not a ``datetime``): the same
    dict rides ``printer_state_to_dict`` through the WebSocket serializer's bare
    ``json.dumps``, which has no encoder behind it.
    """

    # Always a persisted row: the projection cache is filled from committed rows, so
    # the id and the two status words are facts, not optionals.
    id: int
    kind: str
    status: str
    #: the job the hold paused (the printer's ``subtask_id`` at open; ``""`` when it
    #: named none) — read by the terminal classifier, declared so REST and the WS frame
    #: carry the same projection; the card ignores it
    job_id: str = ""
    slot_desc: str | None = None
    created_at: str | None = None
    operator_exits: bool = False
    #: a recovery driver task is live on this printer (projected from the incident
    #: store's liveness registry, never derived from ``status``)
    driver_live: bool = False
    #: the printer's HMS messages RECORDED when this hold opened (empty for a code-less
    #: hold) — always present on the wire; the card shows the ones the live
    #: ``hms_errors`` no longer carries
    printer_messages: list[PrinterMessageInfo] = []
    #: when the row became a person's (ISO, naive UTC) — a plate-check decision hold's window
    #: starts here
    escalated_at: str | None = None
    #: a plate-check episode's last farm stop as its ledger recorded it (``None`` when the farm
    #: sent none, and for every other kind)
    last_stop: PlateCheckStopInfo | None = None
    #: a plate-check decision hold's deadline (ISO, naive UTC) — the hold's timer projects it
    deadline_at: str | None = None


class ServiceHoldEnterResponse(BaseModel):
    """``POST /printers/{id}/service-hold``: the hold's state plus what the quiesce did.

    The two quiesce bools are the API's and the log's record of what this call actually
    changed — a second click on an already-quiet printer answers two Falses. (The
    operator's toast is static copy: it names the hold's effects, not this payload.)

    There is no cooldown bool and no job bool: entering a hold neither ends a cooldown
    (the fans finish their curve and the eject is withheld) nor stops a print (no mode
    verb does — only Stop), so the only value either could carry is False.
    """

    held: bool
    already_held: bool
    eject_stopped: bool
    lease_revoked: bool


class ServiceHoldExitResponse(BaseModel):
    """``DELETE /printers/{id}/service-hold``: did THIS call release a hold.

    False for a printer that was not held — the verb is idempotent, so "nothing to
    release" is a 200 with ``released: false``, never a 404 or a 409.
    """

    released: bool


class PrinterResponse(PrinterBase):
    id: int
    is_active: bool
    nozzle_count: int = 1  # 1 or 2, auto-detected from MQTT
    print_hours_offset: float = 0.0
    external_camera_url: str | None = None
    external_camera_type: str | None = None
    external_camera_enabled: bool = False
    external_camera_snapshot_url: str | None = None  # #1177
    camera_rotation: int = 0  # 0, 90, 180, 270 degrees
    plate_detection_enabled: bool = False
    plate_detection_roi: PlateDetectionROI | None = None
    quarantined: bool = False
    quarantine_reason: str | None = None
    # Maintenance mode, projected from the printer's open ``service_hold`` incident.
    # Filled by ``_serialize_printer`` (the ONE serializer behind both ``GET /printers/``
    # and ``GET /printers/{id}``) — it is process state, not a column, so it cannot come
    # from ``model_validate``'s ORM read.
    service_hold: ServiceHoldState | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_with_roi(cls, printer) -> "PrinterResponse":
        """Create response from ORM model, converting ROI fields to nested object."""
        data = {
            "id": printer.id,
            "name": printer.name,
            "serial_number": printer.serial_number,
            "ip_address": printer.ip_address,
            "model": printer.model,
            "location": printer.location,
            "auto_archive": printer.auto_archive,
            "external_camera_url": printer.external_camera_url,
            "external_camera_type": printer.external_camera_type,
            "external_camera_enabled": printer.external_camera_enabled,
            "external_camera_snapshot_url": printer.external_camera_snapshot_url,
            "camera_rotation": printer.camera_rotation,
            "is_active": printer.is_active,
            "nozzle_count": printer.nozzle_count,
            "print_hours_offset": printer.print_hours_offset,
            "plate_detection_enabled": printer.plate_detection_enabled,
            "quarantined": printer.quarantined,
            "quarantine_reason": printer.quarantine_reason,
            "created_at": printer.created_at,
            "updated_at": printer.updated_at,
        }
        # Build ROI object if any ROI field is set
        if any(
            [
                printer.plate_detection_roi_x is not None,
                printer.plate_detection_roi_y is not None,
                printer.plate_detection_roi_w is not None,
                printer.plate_detection_roi_h is not None,
            ]
        ):
            data["plate_detection_roi"] = PlateDetectionROI(
                x=printer.plate_detection_roi_x or 0.15,
                y=printer.plate_detection_roi_y or 0.35,
                w=printer.plate_detection_roi_w or 0.70,
                h=printer.plate_detection_roi_h or 0.55,
            )
        return cls(**data)


class PrinterResponseWithSecret(PrinterResponse):
    """PrinterResponse + access_code. Returned ONLY to callers with
    PRINTERS_UPDATE (Admin / Operator JWTs, or single-trust auth-disabled mode).

    Viewers and API keys never receive this shape — they get the bare
    PrinterResponse without access_code, since holding the access_code lets
    the caller talk to the printer's MQTT directly and bypass Bambuddy's RBAC.
    """

    access_code: str


class HMSErrorResponse(BaseModel):
    code: str
    attr: int = 0  # Attribute value for constructing wiki URL
    module: int
    severity: int  # 1=fatal, 2=serious, 3=common, 4=info
    actions: list[str] = []  # List of user-facing action keys (e.g. "CHECK_FILAMENT")
    job_id: str | None = None  # Optional job ID for actions that require it (e.g. "CHECK_ASSISTANT")
    # Canonical hex identifier the firmware uses to match HMS-related commands.
    # 16 chars for `hms[]`-array faults (full 64-bit attr+code), 8 chars for
    # `print_error` faults. The frontend echoes this back as
    # HmsActionBody.print_error so we send the firmware-recognised key, not the
    # truncated short_code that historically caused silent command rejection
    # (#1830, H2D wrong-plate verification).
    full_code: str = ""
    # Serialization-time enrichment (services.hms_errors.hms_error_payload) so the
    # frontend never re-derives the code or holds its own description table:
    short_code: str = ""  # Canonical "MMMM_CCCC" (e.g. "0300_400C")
    description: str | None = None  # Vendor fault text; None when the code is unknown
    wiki_url: str = ""  # Bambu HMS wiki landing page


class AMSTray(BaseModel):
    id: int
    tray_color: str | None = None
    tray_type: str | None = None
    tray_sub_brands: str | None = None  # Full name like "PLA Basic", "PETG HF"
    tray_id_name: str | None = None  # Bambu filament ID like "A00-Y2" (can decode to color)
    tray_info_idx: str | None = None  # Filament preset ID like "GFA00"
    remain: int = 0
    k: float | None = None  # Pressure advance value (from tray or K-profile lookup)
    cali_idx: int | None = None  # Calibration index for K-profile lookup
    tag_uid: str | None = None  # RFID tag UID (any tag)
    tray_uuid: str | None = None  # Bambu Lab spool UUID (32-char hex)
    nozzle_temp_min: int | None = None  # Min nozzle temperature
    nozzle_temp_max: int | None = None  # Max nozzle temperature
    drying_temp: int | None = None  # RFID-recommended drying temp
    drying_time: int | None = None  # RFID-recommended drying time (hours)
    state: int | None = None  # AMS tray state: 9=empty, 10=spool present not loaded, 11=loaded


class AMSUnit(BaseModel):
    id: int
    humidity: int | None = None
    temp: float | None = None
    is_ams_ht: bool = False  # True for AMS-HT (single spool), False for regular AMS (4 spools)
    tray: list[AMSTray] = []
    serial_number: str = ""  # AMS unit serial number (sn from MQTT)
    sw_ver: str = ""  # AMS firmware version (from get_version info.module)
    dry_time: int = 0  # Minutes remaining (0 = not drying, >0 = drying active)
    dry_status: int = 0  # 0=Off, 1=Checking, 2=Drying, 3=Cooling, 4=Stopping, 5=Error
    dry_sub_status: int = 0  # 0=Off, 1=Heating, 2=Dehumidify
    dry_sf_reason: list[int] = []  # Cannot-dry reasons from firmware (see CannotDryReason)
    dry_target_temp: int | None = None  # Active-cycle target °C (Bambu doesn't echo this)
    dry_filament: str | None = None  # Active-cycle filament name we sent
    module_type: str = ""  # "ams", "n3f", "n3s"


class NozzleInfoResponse(BaseModel):
    nozzle_type: str = ""  # "stainless_steel" or "hardened_steel"
    nozzle_diameter: str = ""  # e.g., "0.4"


class NozzleRackSlot(BaseModel):
    """H2C nozzle rack slot (6-position tool-changer dock)."""

    id: int = 0
    nozzle_type: str = ""
    nozzle_diameter: str = ""
    wear: int | None = None
    stat: int | None = None  # Nozzle status (e.g. mounted/docked)
    max_temp: int = 0  # Max temperature rating °C (0 = not set)
    serial_number: str = ""  # Nozzle serial number
    filament_color: str = ""  # RGBA hex ("00000000" = no filament)
    filament_id: str = ""  # Bambu filament ID
    filament_type: str = ""  # Material type (e.g. "PLA", "PETG")


class AmsLabelBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    ams_serial: str = Field(default="", max_length=50)


class ConfigureAmsSlotBody(BaseModel):
    """The filament configuration the operator states for one AMS slot.

    Config payload, so it travels as a request body rather than as a dozen query
    parameters. Every field here is an operator STATEMENT and outranks anything the
    farm would infer — with one exception that is the point of the endpoint's
    fallback ladder: an empty ``tray_info_idx`` states nothing, and the identity is
    then composed by ``services.slot_identity.resolve_slot_identity`` exactly as it
    is for every other lane that writes a slot.
    """

    # "GFL05", a "P…" local preset, or "" to let the identity resolver choose.
    tray_info_idx: str = Field(default="", max_length=64)
    tray_type: str = Field(..., min_length=1, max_length=32)
    tray_sub_brands: str = Field(default="", max_length=128)
    # RRGGBBAA / RRGGBB hex; normalised to 8 uppercase digits before it reaches the wire.
    tray_color: str = Field(..., min_length=6, max_length=8, pattern=r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
    nozzle_temp_min: int = Field(..., ge=0, le=500)
    nozzle_temp_max: int = Field(..., ge=0, le=500)
    # -1 selects the firmware's default K (0.020) — see BambuClient.extrusion_cali_sel.
    cali_idx: int = Field(default=-1, ge=-1)
    nozzle_diameter: str = Field(default="0.4", max_length=8)
    setting_id: str = Field(default="", max_length=64)
    kprofile_filament_id: str = Field(default="", max_length=64)
    kprofile_setting_id: str = Field(default="", max_length=64)
    # 0.0 skips the direct extrusion_cali_set write.
    k_value: float = Field(default=0.0, ge=0.0)


class HmsActionBody(BaseModel):
    # Canonical hex identifier (HMSErrorResponse.full_code): 8 chars for
    # `print_error`-sourced faults, 16 chars for `hms[]`-array faults whose
    # full 64-bit code is the firmware's matching key. Length-bounded to
    # those two valid shapes to keep stray input from reaching the dispatcher.
    print_error: str = Field(..., min_length=8, max_length=16, pattern=r"^[0-9A-Fa-f]{8}([0-9A-Fa-f]{8})?$")
    # One of the HMSAction enum values. Length-capped to keep stray input from
    # reaching the dispatcher's `match` statement.
    action: str = Field(..., min_length=1, max_length=64)
    # The `subtask_id` snapshot from the HMSError that surfaced this dialog.
    # Bambu echoes it back in HMS-aware commands. Optional for idle errors.
    job_id: str | None = Field(default=None, max_length=64)


class FilaSwitchResponse(BaseModel):
    """Filament Track Switch (FTS) state — accessory that mediates AMS-to-extruder routing.

    When installed, the AMS info field reports bits 8-11 = 0xE (uninitialized)
    because slots are dynamically routed via the FTS rather than tied to a
    specific extruder. Frontend uses `installed` to suppress the per-extruder
    slot filter in the print modal. See #1162.
    """

    installed: bool = False
    # in[track] = currently loaded slot for that track (-1 = empty)
    in_slots: list[int] = []
    # out[track] = extruder this track terminates at (0 = right, 1 = left)
    out_extruders: list[int] = []
    stat: int = 0
    info: int = 0


class EjectWatchInfo(BaseModel):
    """In-flight eject cooldown watch summary: the eject line (°C) the
    watch armed with — measured shop air plus the one margin since 2026-09-25, None
    when shop air was unknown (the watch still cools and releases on the bed's own air
    or at its plateau) — and, since the cooldown prep (2026-09-10), the Z the plate is
    HELD at for the wait (None when the hold was skipped or the model has no clearance
    numbers). Declared here because the
    REST ``/status`` lane serialises through this model with ``extra="ignore"``
    while the WS lane dumps the same dict raw: a field missing here would flip the
    card's "plate raised" chip between the two lanes (the C5 class).

    ``deferred`` (2026-09-13) is "this plate has finished cooling and its eject is waiting
    on maintenance mode": the fans are already retired, so the hold flag and the watch's
    existence cannot tell that state from a cooldown still in progress."""

    threshold_c: float | None = None
    hold_z: float | None = None
    deferred: bool = False


class PlateRefusalInfo(BaseModel):
    """Why the plate authority holds a REFUSED plate: the printer's plate check paused a
    job that then ended without printing. ``messages`` are the printer's words for the
    check — the stop wiped them off the printer."""

    messages: list[PrinterMessageInfo] = []


class PlateInfo(BaseModel):
    """The deposit standing on a printer's build plate, and what happens to it next."""

    occupied: bool = False
    # The subtask id of the job that raised the gate; None for a source-less raise
    # (a printer-vision trip, an operator declaration) — those are human-clear-only.
    source_subtask_id: str | None = None
    # The policy class name: CooldownEject / FirstArticleEject / ForeignAutoEject /
    # EscalationOnly. A NAME, not its parameters — the UI renders "what will happen to
    # this plate", and the unit/profile behind it is already on the queue surfaces.
    policy: str | None = None
    since: datetime | None = None
    # Set when the gate holds a plate the printer's own plate check REFUSED; None for
    # every other gate. Declared here for the C5 reason: the REST lane validates through
    # this model while the WS lane dumps ``occupancy_payload``'s dict raw.
    refusal: PlateRefusalInfo | None = None


class PendingEjectInfo(BaseModel):
    """An eject sweep this printer is claimed by, until its terminal arrives."""

    purpose: str
    # Has the printer echoed the sweep's PRINT START? False means "dispatched, not yet
    # acknowledged" — the state that used to be indistinguishable from a running sweep
    # and left operators guessing whether a 409 would ever clear.
    started: bool = False
    age_s: float | None = None
    # Rebuilt from the durable stamp at startup rather than minted by a live dispatch:
    # no watchdog, no verifiable identity, and an operator eject supersedes it.
    hydrated: bool = False
    # The runtime watchdog has already given its verdict on this sweep: it fired, and
    # whether or not its stop was delivered, nothing is going to act on this eject now.
    # Declared here for the same reason ``EjectWatchInfo`` declares ``hold_z``: the REST
    # ``/status`` lane validates through this model while the WS lane dumps
    # ``occupancy_payload``'s dict raw, so a field missing here flips the operator's
    # "Eject stalled — Recover" row between the poll and the socket push (the C5 class).
    runtime_exceeded: bool = False


class PlateOccupancyInfo(BaseModel):
    """What the plate-occupancy authority STORES for this printer.

    Stored fields only, deliberately: the authority also projects an OWNER (none /
    dispatch / job / eject), and that projection needs ``db_claim`` — a ``print_queue``
    row read per scheduler tick, which the synchronous, session-less WebSocket
    serializer can never supply. Publishing it from REST alone would make the two
    transports disagree about the same printer, which is the exact flip the
    ``eject_watch`` payload is careful to avoid. The UI derives its phase from
    ``awaiting_plate_clear`` + ``state``, and needs no server-side owner.
    """

    plate: PlateInfo = PlateInfo()
    eject: PendingEjectInfo | None = None
    # Age of the dispatch lease this printer holds (a unit decided-but-not-yet-settled
    # on the wire), or None when it holds none.
    lease_age_s: float | None = None


class PrintOptionsResponse(BaseModel):
    """AI detection and print options from xcam data."""

    # Core AI detectors
    spaghetti_detector: bool = False
    print_halt: bool = False
    halt_print_sensitivity: str = "medium"  # Spaghetti sensitivity
    first_layer_inspector: bool = False
    printing_monitor: bool = False
    buildplate_marker_detector: bool = False
    allow_skip_parts: bool = False
    # Additional AI detectors (decoded from cfg bitmask)
    nozzle_clumping_detector: bool = True
    nozzle_clumping_sensitivity: str = "medium"
    pileup_detector: bool = True
    pileup_sensitivity: str = "medium"
    airprint_detector: bool = True
    airprint_sensitivity: str = "medium"
    auto_recovery_step_loss: bool = True
    filament_tangle_detect: bool = False


class ToolheadRefillState(BaseModel):
    """The farm's refill of an EMPTY toolhead, as the card's "Toolhead empty" chip reads it.

    ``phase`` — ``loading``: a refill (or a recovery driver's load) is in flight on this printer;
    ``failed``: the open row's last command did not reach its end. ``command`` — the motion the phase
    speaks of: ``load`` while loading; the failed step's own command when failed (a failed UNLOAD is
    an unload, never "Load failed"). ``slot`` — the slot it is loading or failed on (an unload names
    no target: the row's slot); ``answer`` — what the AMS answered the failed command
    (``no_movement`` / ``acted``), ``None`` while loading."""

    phase: Literal["loading", "failed"]
    command: AmsMotionCommand
    slot: str | None = None
    answer: str | None = None


class ToolheadState(BaseModel):
    """The printer's ACTIVE extruder feed (``tray_fields.toolhead_feed``, K1) and the farm's refill
    of it — a DERIVED projection, built by ``printer_manager.toolhead_payload`` for both ``/status``
    branches and the WS frame.

    ``feed`` — ``fed`` (a real AMS feeder) / ``external`` (the external spool) / ``empty``
    (nothing fed) / ``unknown`` (nothing read); ``tray`` — the global tray when ``fed``;
    ``refill`` — set only while the feed reads empty or unknown and the farm is loading it or could
    not; ``refill_reason`` — set only while the feed reads EMPTY: what a Resume would do about it
    (:data:`ToolheadRefillReason`, the T3 verdict — a client keys its "Toolhead empty" copy off it and
    never re-derives the verdict)."""

    feed: Literal["fed", "external", "empty", "unknown"]
    tray: int | None = None
    refill: ToolheadRefillState | None = None
    refill_reason: ToolheadRefillReason | None = None


class PrinterStatus(BaseModel):
    id: int
    name: str
    connected: bool
    state: str | None = None
    current_print: str | None = None
    subtask_name: str | None = None
    gcode_file: str | None = None
    progress: float | None = None
    remaining_time: int | None = None
    layer_num: int | None = None
    total_layers: int | None = None
    temperatures: dict | None = None
    cover_url: str | None = None
    hms_errors: list[HMSErrorResponse] = []
    ams: list[AMSUnit] = []
    ams_exists: bool = False
    # AMS exist-bit triage, from the last push that carried the mask. `tray_exist_bits`
    # verbatim as hex; `power_on_flag` as the firmware reported it (RECORDED ONLY — it
    # does not mean "AMS is powered": False is the normal steady state across most of the
    # fleet); `ams_bits_trusted` = whether the farm acted on that mask (an all-zero mask
    # must repeat before it may empty a slot).
    ams_tray_exist_bits: str | None = None
    ams_power_on_flag: bool | None = None
    ams_bits_trusted: bool = False
    vt_tray: list[AMSTray] = []  # Virtual tray / external spool(s)
    sdcard: bool = False  # SD card inserted
    store_to_sdcard: bool = False  # Store sent files on SD card
    timelapse: bool = False  # Timelapse recording active
    ipcam: bool = False  # Live view enabled
    wifi_signal: int | None = None  # WiFi signal strength in dBm
    wired_network: bool = False  # Ethernet connection detected
    door_open: bool = False  # Enclosure door open (X1/P1S/P2S/H2*)
    nozzles: list[NozzleInfoResponse] = []  # Nozzle hardware info (index 0=left/primary, 1=right)
    nozzle_rack: list[NozzleRackSlot] = []  # H2C 6-nozzle tool-changer rack
    print_options: PrintOptionsResponse | None = None  # AI detection and print options
    # Calibration stage tracking
    stg_cur: int = -1  # Current stage number (-1 = not calibrating)
    stg_cur_name: str | None = None  # Human-readable current stage name
    stg: list[int] = []  # List of stage numbers in calibration sequence
    # Air conditioning mode (0=cooling, 1=heating)
    airduct_mode: int = 0
    # Print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)
    speed_level: int = 2
    # Chamber light on/off
    chamber_light: bool = False
    # Active extruder for dual nozzle (0=right, 1=left)
    active_extruder: int = 0
    # AMS mapping for dual nozzle: which AMS is connected to which nozzle
    ams_mapping: list[int] = []
    # Per-AMS extruder map: {ams_id: extruder_id} where 0=right, 1=left
    ams_extruder_map: dict[str, int] = {}
    # Filament Track Switch (FTS) accessory — when installed, AMS reports
    # bits 8-11 = 0xE (uninitialized) and routing is dynamic via the FTS. See #1162.
    fila_switch: FilaSwitchResponse | None = None
    # Currently loaded tray (global ID): 254 = external spool, 255 = no filament
    tray_now: int = 255
    # AMS status for filament change tracking
    # Main status: 0=idle, 1=filament_change, 2=rfid_identifying, 3=assist, 4=calibration
    ams_status_main: int = 0
    # Sub status: specific step within filament change (when main=1)
    # Known values: 4=retraction, 6=load verification, 7=purge
    ams_status_sub: int = 0
    # mc_print_sub_stage - filament change step indicator used by OrcaSlicer/BambuStudio
    mc_print_sub_stage: int = 0
    # Timestamp of last AMS data update (for RFID refresh detection)
    last_ams_update: float = 0.0
    # Number of printable objects in current print (for skip objects feature)
    printable_objects_count: int = 0
    # Fan speeds (0-100 percentage, None if not available for this model)
    cooling_fan_speed: int | None = None  # Part cooling fan
    big_fan1_speed: int | None = None  # Auxiliary fan
    big_fan2_speed: int | None = None  # Chamber/exhaust fan
    heatbreak_fan_speed: int | None = None  # Hotend heatbreak fan
    # Firmware version (from info.module[name="ota"].sw_ver)
    firmware_version: str | None = None
    # Developer LAN mode: True = enabled, False = disabled (MQTT encryption), None = unknown
    developer_mode: bool | None = None
    # AMS Filament Backup ("auto-switch" to a second spool when one runs out).
    # True = ON, False = OFF, None = unknown / unsupported (A1 family — protocol field
    # not yet identified). UI treats None as "status unavailable", not as a hard disable.
    ams_filament_backup: bool | None = None
    # Queue: printer is awaiting the user to acknowledge the build plate is cleared
    # after a finished/failed print. Persisted across restarts (#961).
    awaiting_plate_clear: bool = False
    # Farm failure policy: printer quarantined after consecutive failures, excluded
    # from dispatch until an operator clears it.
    quarantined: bool = False
    quarantine_reason: str | None = None
    # Farm device reconciliation: the device's self-reported model differs
    # from the declared Printer.model — the scheduler blocks dispatch until the
    # declaration is corrected. Absent device report ⇒ never a mismatch.
    model_mismatch: bool = False
    model_mismatch_reason: str | None = None
    # Cooldown/eject phase: the in-flight eject cooldown watch's
    # release threshold; the UI renders "Cooling to T °C (bed B °C)" while set.
    # None when no threshold-bearing watch is armed.
    eject_watch: EjectWatchInfo | None = None
    # Plate-occupancy authority projection (2026-08-30). ADDITIVE beside
    # ``awaiting_plate_clear``, which stays: nine frontend consumers read that boolean
    # and it remains the phase input. This carries the WHY — which policy holds the
    # plate, since when, and whether an eject is in flight and has actually started.
    occupancy: PlateOccupancyInfo | None = None
    # Maintenance mode (2026-09-12): a human has this printer and every automatic lane
    # stands down. Reportable with or without a session — it is the incident store's
    # own record, not a wire fact — so BOTH ``/status`` branches carry it, and so does
    # ``printer_state_to_dict``'s WS frame from the same builder.
    service_hold: ServiceHoldState | None = None
    # The open equipment-fault row (2026-09-17). Reportable with or without a session
    # for the same reason maintenance mode is — it is the incident store's own record,
    # not a wire fact — so BOTH ``/status`` branches carry it, and so does
    # ``printer_state_to_dict``'s WS frame, from the one builder.
    open_incident: OpenIncidentState | None = None
    # The plate-check human's turn (operator ruling 2026-10-05): the buttons a person may press
    # on the printer's plate-check dialog through the farm, and when the farm stops the print
    # if nobody does. Same builder for both ``/status`` branches and the WS frame.
    plate_check_exit: PlateCheckExit | None = None
    # The ACTIVE extruder's feed and the farm's refill of it (K10/C3c, 2026-10-10): what the card's
    # "Toolhead empty" chip reads. Same builder for both ``/status`` branches and the WS frame.
    toolhead: ToolheadState | None = None
    # AMS drying support
    supports_drying: bool = False
    # AMS "Print While Drying" — drying mid-print. Verified per Bambu wiki release notes;
    # see _DRY_WHILE_PRINTING_MIN_FIRMWARE in printer_manager.py for the matrix.
    supports_drying_while_printing: bool = False
    # Active chamber heater (responds to M141). True only for H2C/H2D/H2DPro/H2S/X2D.
    supports_chamber_heater: bool = False
    # Chamber (exhaust) fan — M106 P3 moves air. True on every ENCLOSED model;
    # false on the open-frame A1 family, A2L and P1P.
    has_chamber_fan: bool = False
    # Switchable cooling/heating air duct (M145 P0/P1, JSON set_airduct).
    # True only for P2S/X2D and the H2 family.
    supports_airduct: bool = False
    # Linked archive for the active print (resolved via subtask_id). Frontend uses
    # this to fetch plate metadata and show the plate name when the source 3MF is
    # multi-plate (#881 follow-up).
    current_archive_id: int | None = None
    # 1-indexed plate number parsed from gcode_file (e.g. /Metadata/plate_2.gcode).
    # Set for every active print regardless of plate count; the frontend decides
    # whether to render it based on current_archive_id's is_multi_plate flag.
    current_plate_id: int | None = None


class RecoverResult(BaseModel):
    """What ``POST /printers/{id}/recover`` actually changed.

    Every field reports whether that state was really mutated, so a repeat call (the
    verb is idempotent) is visibly a no-op rather than a second success.

    ``incidents_closed`` carries the KINDS of the equipment-fault rows the verb ended.
    It exists because Recover's effect on a hold was invisible until 2026-09-17: the
    operator pressed it against 011-H2S's escalated physical row, got
    ``{plate_cleared: false, quarantine_cleared: false, runs_resumed: []}`` back, and
    had no way to tell "it closed the fault" from "it did nothing at all".
    """

    plate_cleared: bool = False
    quarantine_cleared: bool = False
    runs_resumed: list[int] = []
    incidents_closed: list[str] = []


class ClearPlateResult(BaseModel):
    """What ``POST /printers/{id}/clear-plate`` did, in the same vocabulary.

    The routine plate ack answers the ``operator`` class only — a Z datum lost to a
    reboot — never a filament-path hold or a paused plate check; ``incidents_closed``
    is how the operator sees WHICH, instead of inferring it from a chip that did or did
    not go dark.
    """

    success: bool = True
    message: str = ""
    incidents_closed: list[str] = []


class DiagnosticCheck(BaseModel):
    """One connection-diagnostic check result.

    ``id`` is a stable key (port_mqtt, port_ftps, port_rtsps, network_mode,
    subnet, mqtt_auth, developer_mode); the frontend renders the localized
    title and fix text from id + status. ``params`` carries interpolation
    values (e.g. network mode, IP addresses) for that text.
    """

    id: str
    status: str  # "pass" | "fail" | "warn" | "skip"
    params: dict = Field(default_factory=dict)


class PrinterDiagnosticResult(BaseModel):
    """Result of a printer connection diagnostic run."""

    printer_id: int | None = None
    ip_address: str
    overall: str  # "ok" | "warnings" | "problems"
    checks: list[DiagnosticCheck]


class DiagnosticRequest(BaseModel):
    """Pre-save (Add Printer) connection diagnostic request.

    serial_number + access_code are optional: when both are present the
    diagnostic also probes MQTT credentials, otherwise only the
    network-level checks run.
    """

    ip_address: str
    serial_number: str | None = None
    access_code: str | None = None


class SlotRecheckResponse(BaseModel):
    """What "Re-check slot" concluded — the sentence the operator actually gets.

    The endpoint this replaces returned a bare 200/400 with nothing renderable, and THAT is
    the bug it exists to close (incident shape 32: 21 minutes of clicking against silence).
    Every outcome in doctrine rule 12's contract carries a verdict here and the UI renders
    exactly one sentence per verdict in a ``role="status"`` live region.

    ``verdict`` values: ``unchanged`` (R1) | ``minted`` (R2/R3/R5/R8) | ``identified`` (R4's
    tag-found half) | ``queued`` (R3/R4 mid-print) | ``empty`` (R6) | ``restored`` (the
    acknowledgement's undo).

    The wire type is CLOSED (``RecheckOutcome``), and deliberately so: the frontend's
    ``recheckSentence`` ends in a ``default:`` arm that renders an unrecognised verdict as
    "nothing moved in this slot", so a bare ``str`` here would let a new outcome reach the
    operator as the exact false no-op — the original silence bug — instead of failing loudly
    at the boundary. Built field-for-field from ``slot_recheck.RecheckVerdict``; the service
    decides, this only carries.
    """

    verdict: RecheckOutcome
    printer_id: int
    ams_id: int
    tray_id: int
    spool_id: int | None = None
    label_weight_g: float | None = None
    brand: str | None = None
    material: str | None = None
    # Did a read actually go out on the wire for this click? The tagged-slot refresh and the
    # tagless-slot discovery read both report here, and a refusal (drying, an identify in
    # flight, filament engaged, the ask pace) reports False rather than being swallowed —
    # the operator must be able to tell "the farm asked" from "the farm could not ask".
    read_issued: bool = False
    undo_available: bool = False


class AmsCommandResponse(BaseModel):
    """What ``POST /printers/{id}/ams/load`` and ``/ams/unload`` answer with a 200.

    ``outcome`` is the wire's measured answer (``ams_command.classify``) — the UI keys its
    toast off it. ``message`` is the non-UI-client fallback sentence only. Closed type for
    ``SlotRecheckResponse``'s reason: a bare ``str`` would let a new outcome reach a client
    unannounced.
    """

    outcome: AmsCommandOutcome
    message: str
    #: the posture family the command was sent into (``ams_command.AmsCommandResult.family``) —
    #: ``None`` only for a refusal, which never reaches a 200
    family: AmsPostureFamily | None = None


class PrintResumeResponse(BaseModel):
    """What ``POST /printers/{id}/print/resume`` answers with a 200 (K9, 2026-10-10).

    ``status`` — ``resumed``: the resume went out (the toolhead read fed, or the firmware feeds it
    itself — a same-slot runout demand, its own filament change, the start block); ``refilling``:
    the toolhead read EMPTY, so the farm refills it first as the printer's recovery driver and
    resumes once the load reached the toolhead — answered at once, the outcome arriving through the
    status projection (``PrinterStatus.toolhead``) and, on a failure, a page. ``slot`` — the slot
    the refill loads first ("AMS A slot 1"), ``None`` when the swap's selection decides (and for
    ``resumed``). ``message`` is the non-UI-client fallback; a client keys its copy off ``status``.
    """

    success: bool = True
    status: Literal["resumed", "refilling"]
    slot: str | None = None
    message: str


class PrintResumeRefusal(BaseModel):
    """The 409 ``detail`` of a refused resume (K9): nothing was sent, and the print is never
    resumed onto an empty toolhead. ``reason`` (closed, :data:`ResumeRefusalReason`) is what a
    client keys its copy off; ``slot`` / ``answer`` name a slot and an AMS answer when the refusal
    has one (none of today's reasons does — a refill's slot and answer reach the client through the
    status projection); ``message`` is the fallback sentence."""

    reason: ResumeRefusalReason
    slot: str | None = None
    answer: str | None = None
    message: str
