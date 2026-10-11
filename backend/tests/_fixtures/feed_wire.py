"""Drive a REAL ``BambuMQTTClient`` over the wire the toolhead feed state reads.

The feed state (``services/feed_state``, invariant 16) is stepped by the MQTT client alone,
once per status push (``BambuMQTTClient._step_feed``), so a consumer that asks it a question
— did the firmware switch rolls, which roll drained, what fed last — is tested by driving
the client's own ``_process_message`` with the pushes a printer sends, never by a reading
built by hand. ONE driver, so the wire shape cannot drift between the consumers' files.

The pushes are the H2S fleet's as measured on 2026-10-10: a status push carries the job's
``layer_num``, ``ams_status`` (``STEADY`` — main status 3, assist, the steady state of every
healthy RUNNING H2S; ``CHANGE`` — main status 1, the change posture) and the ``ams`` block
with every unit's four trays and ``tray_now``. A drained bay reads state 9 with no type.
A dual-nozzle push adds ``device.extruder.info[]`` with each extruder's ``snow`` and the
active extruder in bit 8 of ``device.extruder.state``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient

STEADY = 0x0300  # ams_status_main 3 (assist): every RUNNING H2S reads it
CHANGE = 0x0105  # ams_status_main 1: the change posture

NOTHING_FED = 255


def live_client(*, serial: str = "FEEDWIRE", model: str = "H2S") -> BambuMQTTClient:
    """A connected client, its job callbacks wired (a terminal fires and ends the job).

    The session's FIRST applied report makes its readings live (``live_reading.reads_live``),
    so the first push a test sends seeds the feed state and the second is the first evidence.
    """
    client = BambuMQTTClient(ip_address="192.168.1.50", serial_number=serial, access_code="12345678", model=model)
    client._client = MagicMock()
    client.state.connected = True
    client.on_print_start = lambda data: None
    client.on_print_complete = lambda data: None
    return client


def ams_units(units: Iterable[int] = (0,), *, absent: Iterable[int] = ()) -> list[dict]:
    """Every unit's four trays, each seated unless its GLOBAL tray id is named ``absent``."""
    gone = set(absent)
    return [
        {
            "id": str(unit),
            "tray": [
                {"id": str(slot), "state": 9, "tray_type": ""}
                if unit * 4 + slot in gone
                else {"id": str(slot), "state": 11, "tray_type": "PETG"}
                for slot in range(4)
            ],
        }
        for unit in units
    ]


def _snow(tray: int) -> int:
    """A global tray as the firmware's per-extruder ``snow`` spells it (unit << 8 | slot)."""
    if tray == NOTHING_FED:
        return 0xFFFF
    return (tray // 4) << 8 | tray % 4


def status_push(
    *,
    tray: int,
    layer: int,
    units: Iterable[int] = (0,),
    absent: Iterable[int] = (),
    ams_status: int | None = STEADY,
    extruders: Mapping[int, int] | None = None,
    active: int = 0,
    **fields: Any,
) -> dict:
    """ONE status push. ``tray`` is ``tray_now``; ``extruders`` (dual nozzle) maps each
    extruder to the global tray it holds, ``active`` names the printing one."""
    body: dict[str, Any] = {
        "layer_num": layer,
        "ams": {"ams": ams_units(units, absent=absent), "tray_now": str(tray)},
        **fields,
    }
    if ams_status is not None:
        body["ams_status"] = ams_status
    if extruders is not None:
        body["device"] = {
            "extruder": {
                "info": [{"id": extruder, "snow": _snow(held)} for extruder, held in sorted(extruders.items())],
                "state": active << 8,
            }
        }
    return {"print": body}


def push(client: BambuMQTTClient, **kw: Any) -> None:
    """Deliver :func:`status_push` to the client — the feed state steps once."""
    client._process_message(status_push(**kw))


def start_print(
    client: BambuMQTTClient,
    tray: int,
    *,
    subtask_id: str = "job-A",
    total_layers: int = 167,
    **kw: Any,
) -> None:
    """A print this client watches START on ``tray`` (the push that opens the job), then its
    first printed layer — so the job is live and fed when the test's own pushes begin."""
    client._previous_gcode_state = client.state.state or "IDLE"
    push(
        client,
        tray=tray,
        layer=0,
        gcode_state="RUNNING",
        gcode_file=f"{subtask_id}.3mf",
        subtask_id=subtask_id,
        total_layer_num=total_layers,
        **kw,
    )
    push(client, tray=tray, layer=1, **kw)


@dataclass(frozen=True)
class AutoSwitch:
    """One firmware runout AUTO-SWITCH as the fleet sent it (global tray ids): ``drained``
    runs dry at the AMS while it feeds, ``tray_now`` optionally PRE-FLIPS to ``pre_flip``
    ~1 s later (the pre-flip target need not be the roll that lands), and the backup's load
    lands on ``landed`` minutes later, at ``layer``."""

    printer: str
    drained: int
    pre_flip: int | None
    landed: int
    layer: int


# The three switches of 2026-10-10 the per-push air detector broke into (plan table).
AUTO_SWITCHES_20261010 = (
    AutoSwitch("005-H2S", drained=1, pre_flip=0, landed=0, layer=101),
    AutoSwitch("015-H2S", drained=0, pre_flip=None, landed=1, layer=5),
    AutoSwitch("001-H2S", drained=1, pre_flip=0, landed=2, layer=125),
)


def run_auto_switch(
    client: BambuMQTTClient,
    switch: AutoSwitch,
    *,
    after: Callable[[str], None] = lambda step: None,
    **kw: Any,
) -> None:
    """The firmware's switch, one push at a time, from a job already feeding
    ``switch.drained``; ``after(step)`` runs after EVERY push, named by what it carried:

    * ``drained`` — the bay clears under the feeding roll (the feed state's TAIL);
    * ``pre_flip`` — ``tray_now`` flips to the pre-flip target (when the printer does);
    * ``tail`` — the tail feeds on for minutes, layers advancing;
    * ``empty`` — ``tray_now`` reads 255 with the posture not yet up;
    * ``posture`` — the change posture;
    * ``landed`` — the backup's load lands under the posture;
    * ``settled`` — the posture drops: the switch is complete;
    * ``printing`` — the next layer prints on the backup.
    """
    x, layer = switch.drained, switch.layer
    gone = {x} | set(kw.pop("absent", ()))
    held = switch.pre_flip if switch.pre_flip is not None else x

    def step(name: str, **wire: Any) -> None:
        push(client, **{"absent": gone, **kw, **wire})
        after(name)

    step("fed", tray=x, layer=layer - 1, absent=())
    step("drained", tray=x, layer=layer)
    if switch.pre_flip is not None:
        step("pre_flip", tray=switch.pre_flip, layer=layer)
    for tail_layer in (layer + 1, layer + 2):
        step("tail", tray=held, layer=tail_layer)
    change_layer = layer + 2  # the layer does not advance during the change
    step("empty", tray=NOTHING_FED, layer=change_layer)
    step("posture", tray=NOTHING_FED, layer=change_layer, ams_status=CHANGE)
    step("landed", tray=switch.landed, layer=change_layer, ams_status=CHANGE)
    step("settled", tray=switch.landed, layer=change_layer)
    step("printing", tray=switch.landed, layer=change_layer + 1)
