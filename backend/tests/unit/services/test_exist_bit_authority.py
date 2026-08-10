"""``tray_exist_bits`` as the presence authority, pinned against REAL wire payloads.

Every fixture here is a shape captured off the live fleet on 2026-08-10, over raw MQTT,
because the farm's own capture lane could not be trusted to show the wire (see
``TestTheCaptureLaneShowsTheWire`` at the bottom — it was serving the merged copy back).
What that investigation established, and what these tests defend:

* every ~1 Hz status push carries ``print.ams`` as a DICT with ``tray_exist_bits``, and
  the mask was correct against physical reality on every printer sampled;
* ``power_on_flag`` is NOT a statement about AMS power — ``False`` is the ordinary steady
  state on most of the fleet, ``True`` on the rest, with both reporting truthfully — so
  the guard built on it was discarding correct all-empty masks forever;
* a stable-EMPTY tray degrades to a keyless ``{"id": N}`` stub (printer 1) or a minimal
  ``{"id": N, "state": 9}``: nothing in the tray block asserts emptiness, so without the
  mask the slot reads UNKNOWN at 1 Hz forever and release-on-empty cannot fire. It never
  had, in production, for any slot.

The consequence chain the mask closes, and the one it opens, are both destructive in one
direction only — which is why a SET bit is believed at once and an all-zero mask must
repeat first.
"""

import copy

import pytest

from backend.app.services.bambu_mqtt import (
    _ZERO_EXIST_BITS_TRUST_PUSHES,
    BambuMQTTClient,
    MQTTLogEntry,
    apply_tray_exist_bits,
)
from backend.app.services.tray_fields import (
    TRAY_STATE_DIALECT,
    TRAY_STATE_EMPTY,
    TRAY_STATE_FED,
    TRAY_STATE_SEATED,
    TRAY_STATE_TRANSITIONAL,
    TRAY_STATE_UNREPORTED,
    slot_exist_bit,
    tray_presence,
    unit_exist_bit_set,
)
from backend.app.services.tray_observation import observe_ams_push

# --- the wire ---------------------------------------------------------------


def _ams_push(trays, *, bits="0", power_on=False, ams_exist_bits="1", ams_id=0):
    """The sibling-field envelope EVERY sampled printer sends, verbatim.

    Only ``ams``/``tray_exist_bits``/``power_on_flag``/``ams_exist_bits`` vary per case;
    the rest is carried because a parser that only ever meets a trimmed payload is not
    being tested against the wire.
    """
    return {
        "ams": [{"id": ams_id, "tray": trays}],
        "ams_exist_bits": ams_exist_bits,
        "ams_exist_bits_raw": ams_exist_bits,
        "cali_id": 255,
        "cali_stat": 0,
        "insert_flag": True,
        "power_on_flag": power_on,
        "tray_exist_bits": bits,
        "tray_is_bbl_bits": "0",
        "tray_now": "255",
        "tray_pre": "255",
        "tray_read_done_bits": bits,
        "tray_reading_bits": "0",
        "tray_tar": "255",
        "unbind_ams_stat": 0,
        "version": 1,
    }


#: Printer 1 (H2S, prod): four EMPTY trays, each reduced to a keyless stub. `power_on_flag`
#: reads True here; on printers 3-6 the identical all-empty truth arrives with it False.
PRINTER_1_KEYLESS = [{"id": str(i)} for i in range(4)]

#: The other stable-empty spelling seen on the fleet — a state, still no content assertion.
PRINTER_1_MINIMAL = [{"id": str(i), "state": "9"} for i in range(4)]

#: Printer 2: T1 loaded only (mask '2'). A full ~23-key tray block, trimmed to the members
#: the presence/identity lanes read.
LOADED_TRAY = {
    "id": "1",
    "state": 11,
    "tray_type": "PETG",
    "tray_color": "00FF00FF",
    "tray_info_idx": "GFG02",
    "tag_uid": "3CF1F3E700000100",
    "remain": 42,
    "nozzle_temp_min": 220,
    "nozzle_temp_max": 260,
}


@pytest.fixture
def client():
    """A real client with the production raw hook replaced by a recorder."""
    from unittest.mock import MagicMock

    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST_H2S", access_code="12345678")
    captured: list = []
    c.on_ams_push_raw = captured.append
    c._client = MagicMock()
    c.state.connected = True
    c.captured = captured  # type: ignore[attr-defined]
    return c


def _observed(client, ams_id=0, tray_id=0):
    """The observation the PRODUCTION lane would build from the last hand-off."""
    for obs in observe_ams_push(1, client.captured[-1]):
        if obs.ams_id == ams_id and obs.tray_id == tray_id:
            return obs
    raise AssertionError(f"no observation for A{ams_id}-T{tray_id}")


# --- 1. the presence contract table -----------------------------------------


@pytest.mark.parametrize("state", [None, TRAY_STATE_EMPTY, TRAY_STATE_UNREPORTED, *TRAY_STATE_DIALECT])
@pytest.mark.parametrize("tray_type", [None, "", "PETG"])
def test_a_set_bit_is_presence_for_every_non_present_state(state, tray_type):
    """The bit is the firmware's answer. It subsumes the 003-H2S stuck-9 mid-print insert
    and every dialect whose ``state`` never reports presence at all."""
    assert tray_presence(state, tray_type, exist_bit=True) is True


@pytest.mark.parametrize("state", [TRAY_STATE_SEATED, TRAY_STATE_FED])
@pytest.mark.parametrize("tray_type", [None, "", "PETG"])
def test_a_set_bit_agrees_with_a_present_state(state, tray_type):
    assert tray_presence(state, tray_type, exist_bit=True) is True


@pytest.mark.parametrize(
    "state",
    [None, TRAY_STATE_EMPTY, TRAY_STATE_UNREPORTED, *TRAY_STATE_DIALECT, *TRAY_STATE_TRANSITIONAL],
)
@pytest.mark.parametrize("tray_type", [None, "", "PETG"])
def test_a_clear_bit_empties_every_non_present_state(state, tray_type):
    """Release-authorizing, and it has to be: on the stable-empty shapes NOTHING else in
    the tray block asserts emptiness, which is why release-on-empty never fired."""
    assert tray_presence(state, tray_type, exist_bit=False) is False


@pytest.mark.parametrize("state", [TRAY_STATE_SEATED, TRAY_STATE_FED])
@pytest.mark.parametrize("tray_type", [None, "", "PETG"])
def test_a_clear_bit_against_a_present_state_is_the_in_push_contradiction(state, tray_type):
    """The push disagrees with itself. A release needs UNCONTRADICTED emptiness, so
    neither side wins and the slot stays unknown — unknown fails open everywhere."""
    assert tray_presence(state, tray_type, exist_bit=False) is None


@pytest.mark.parametrize(
    ("state", "tray_type", "expected"),
    [
        (TRAY_STATE_SEATED, None, True),
        (TRAY_STATE_FED, "PETG", True),
        (TRAY_STATE_EMPTY, "", False),
        (TRAY_STATE_EMPTY, None, None),
        (TRAY_STATE_EMPTY, "PETG", None),
        (TRAY_STATE_UNREPORTED, None, None),
        (TRAY_STATE_DIALECT[0], None, None),
        (None, None, None),
    ],
)
def test_without_a_bit_the_state_rules_are_untouched(state, tray_type, expected):
    assert tray_presence(state, tray_type, exist_bit=None) is expected


# --- 2/3. client replay: the prod all-empty shapes ---------------------------


@pytest.mark.parametrize(
    ("trays", "before_trust"),
    [
        pytest.param(PRINTER_1_KEYLESS, None, id="keyless"),
        pytest.param(PRINTER_1_MINIMAL, False, id="minimal_state_9"),
    ],
)
@pytest.mark.parametrize("power_on", [True, False], ids=["printer1_power_on", "printer4_power_off"])
def test_the_all_empty_shape_reads_empty_once_the_mask_has_repeated(client, trays, before_trust, power_on):
    """Both stable-empty spellings, both ``power_on_flag`` polarities, one answer.

    The flag is the point of the parametrization: printer 1 sent True and printers 3-6
    sent False for the same physical truth, so any behaviour that differs between them is
    a bug — the old guard's was to discard the second group's report forever.

    ``before_trust`` is where the two spellings genuinely differ. A ``{id, state: 9}``
    partial has a SECOND, mask-free route to emptiness (``_normalize_cleared_trays``
    injects the asserted-cleared shape, the fallback tier for dialects that carry no mask),
    so it answers before the streak matures. The KEYLESS stub has no such route: nothing in
    it asserts anything, which is why those slots sat UNKNOWN at 1 Hz for two days.
    """
    push = _ams_push(copy.deepcopy(trays), bits="0", power_on=power_on)

    for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES - 1):
        client._handle_ams_data(copy.deepcopy(push))
        assert _observed(client).present is before_trust
        assert _observed(client).exist_bit is None, "an unrepeated all-zero mask is withheld"

    client._handle_ams_data(copy.deepcopy(push))
    for tray_id in range(4):
        obs = _observed(client, tray_id=tray_id)
        assert obs.exist_bit is False
        assert obs.present is False, "the firmware's answer, finally acted on"


def test_a_stale_state_ten_tray_demotes_to_the_full_cleared_shape(client):
    """Printer 4's merged state: trays left at state 10 from an old report while the
    firmware has been reporting all-empty. The merge must write the WHOLE cleared shape,
    not just the state — a tray whose content keys stay ABSENT reads UNKNOWN to the
    backend while the frontend (which keys on ``state === 9``) paints it empty, and a
    display-versus-decision split about the same slot is how a phantom binding survives.
    """
    client._handle_ams_data(
        _ams_push(
            [
                {"id": str(i), "state": 10, "tray_type": "PETG", "tray_color": "00FF00FF", "remain": 55}
                for i in range(4)
            ],
            bits="f",
            power_on=False,
        )
    )
    assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PETG"

    empty = _ams_push(copy.deepcopy(PRINTER_1_KEYLESS), bits="0", power_on=False)
    for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES):
        client._handle_ams_data(copy.deepcopy(empty))

    for tray in client.state.raw_data["ams"][0]["tray"]:
        assert tray["state"] == 9
        assert tray["tray_type"] == ""
        assert tray["tray_color"] == ""
        assert tray["tray_info_idx"] == ""
        assert tray["remain"] == 0
        assert tray["tag_uid"] == "0000000000000000"
    assert _observed(client).present is False


# --- 4/5. the two asymmetric edges ------------------------------------------


def test_an_insert_is_believed_on_its_first_push(client):
    """No streak for a SET bit: believing a spool is there destroys nothing, and the roll
    the farm cannot see is the roll it prints over."""
    empty = _ams_push(copy.deepcopy(PRINTER_1_KEYLESS), bits="0")
    for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES):
        client._handle_ams_data(copy.deepcopy(empty))
    assert _observed(client).present is False

    client._handle_ams_data(_ams_push([{"id": "0", "state": 9}], bits="1"))
    obs = _observed(client)
    assert obs.exist_bit is True
    assert obs.present is True, "003-H2S: state sticks at 9 while the mask already reports the spool"


def test_one_anomalous_zero_frame_never_emits_an_empty_observation(client):
    """'f' … '0' … 'f'. The single zero must reach NO consumer as emptiness — the streak
    is consecutive, so the run breaks and starts over."""
    loaded = _ams_push([copy.deepcopy(LOADED_TRAY)], bits="2", ams_id=0)
    client._handle_ams_data(copy.deepcopy(loaded))
    client._handle_ams_data(_ams_push([{"id": "1"}], bits="0"))
    client._handle_ams_data(copy.deepcopy(loaded))

    assert all(obs.present is not False for push in client.captured for obs in observe_ams_push(1, push))
    assert client._zero_exist_bits_streak == 0
    assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "PETG"


# --- 6/7. what never becomes evidence ---------------------------------------


def test_a_unit_absent_from_ams_exist_bits_gets_no_bit_evidence(client):
    """Its slice of the tray mask is zero because the unit is not being described. Reading
    those zeros as four empty trays would invent the one answer that authorizes a release."""
    push = _ams_push(copy.deepcopy(PRINTER_1_KEYLESS), bits="0", ams_exist_bits="0")
    for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES):
        client._handle_ams_data(copy.deepcopy(push))

    obs = _observed(client)
    assert obs.exist_bit is None
    assert obs.present is None
    # Absent ams_exist_bits gates nothing — unknown fails OPEN, it does not fail closed.
    assert unit_exist_bit_set(None, 0) is False
    for tray_id in range(4):
        assert slot_exist_bit(0b1111, 1, tray_id) is not None


def test_an_untrusted_mask_never_reaches_the_raw_hook(client):
    """Trust is stateful, so the observation layer cannot judge it — the client withholds
    the key instead, and an untrusted push becomes indistinguishable from one that carried
    no mask at all. The unit and tray dicts must still be the very objects the merge is
    about to mutate, so the withholding is a SHALLOW copy."""
    push = _ams_push(copy.deepcopy(PRINTER_1_KEYLESS), bits="0")

    for _ in range(_ZERO_EXIST_BITS_TRUST_PUSHES - 1):
        client._handle_ams_data(copy.deepcopy(push))
        assert "tray_exist_bits" not in client.captured[-1]
        assert client.captured[-1]["ams_exist_bits"] == "1", "only the withheld key is removed"

    client._handle_ams_data(copy.deepcopy(push))
    assert client.captured[-1]["tray_exist_bits"] == "0"


def test_ams_ht_and_out_of_range_slots_are_unaddressable_not_empty():
    """AMS-HT (id >= 128) uses a different scheme and a tray id past the four-per-unit
    stride would read a NEIGHBOURING unit's bit. Both must answer "no evidence" now that
    a clear bit is destructive."""
    assert slot_exist_bit(0, 128, 0) is None
    assert slot_exist_bit(0, 0, 4) is None
    assert slot_exist_bit(0, 0, -1) is None
    assert slot_exist_bit(None, 0, 0) is None
    assert slot_exist_bit(0, 0, 0) is False

    units = [{"id": 128, "tray": [{"id": 0, "state": 11, "tray_type": "PLA"}]}]
    assert apply_tray_exist_bits(units, "0") == 0
    assert units[0]["tray"][0]["tray_type"] == "PLA"


# --- E1. the capture lane shows the wire ------------------------------------


class TestTheCaptureLaneShowsTheWire:
    """``GET /printers/{id}/logging`` must serve what arrived, not what the farm concluded.

    The log stored the parsed payload BY REFERENCE, and two later steps mutate exactly that
    object: ``_handle_ams_data`` normalizes and merges the tray dicts inside it, and
    ``_update_state`` adopted it as ``state.raw_data`` and grafted the MERGED ams list on
    top. The served entry therefore showed the farm's merged copy where the wire's ams dict
    had stood — which is how the 2026-08-10 investigation had to be re-run over raw MQTT.
    """

    def test_a_stored_entry_is_immune_to_later_mutation(self, client):
        client.enable_logging(True)
        payload = {"print": _ams_push([copy.deepcopy(LOADED_TRAY)], bits="2")}

        client._message_log.append(
            MQTTLogEntry(timestamp="t", topic="device/X/report", direction="in", payload=copy.deepcopy(payload))
        )
        # Everything the pipeline does to the payload after the append.
        payload["print"]["ams"][0]["tray"][0]["tray_type"] = "MERGED"
        payload["print"]["ams"] = ["grafted"]
        payload["print"]["tray_exist_bits"] = "ff"

        served = client.get_logs()[-1].payload
        assert served["print"]["ams"][0]["tray"][0]["tray_type"] == "PETG"
        assert served["print"]["tray_exist_bits"] == "2"

    def test_the_real_inbound_path_snapshots(self, client):
        """Through ``_on_message`` itself: the same push the merge is about to rewrite."""
        import json
        from types import SimpleNamespace

        client.enable_logging(True)
        # The real nesting: print.ams is the AMS DICT, whose own "ams" key holds the units.
        wire = {
            "print": {
                "command": "push_status",
                "ams": _ams_push([{"id": "0", "state": 10, "tray_type": "PETG"}], bits="1"),
            }
        }
        client._on_message(
            None,
            None,
            SimpleNamespace(topic=client.topic_subscribe, payload=json.dumps(wire).encode()),
        )

        logged = client.get_logs()[-1].payload["print"]["ams"]
        assert isinstance(logged, dict), "the wire's ams dict, not the farm's merged list"
        assert logged["tray_exist_bits"] == "1"
        assert logged["power_on_flag"] is False
        assert logged["ams"][0]["tray"][0]["tray_type"] == "PETG"

    def test_raw_data_does_not_alias_the_wire_payload(self, client):
        """E2: ``_update_state`` grafts the merged ams/vt_tray back onto the object it
        adopts, so adopting the parsed payload by reference published those grafts into
        anything else still holding it."""
        client.state.raw_data["ams"] = [{"id": 0, "tray": [{"id": 0, "tray_type": "MERGED"}]}]
        data = {"gcode_state": "IDLE", "ams": {"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "WIRE"}]}]}}

        client._update_state(data)

        assert data["ams"]["ams"][0]["tray"][0]["tray_type"] == "WIRE", "the caller's dict is untouched"
        assert client.state.raw_data["ams"][0]["tray"][0]["tray_type"] == "MERGED"


# --- E3. the triage surface --------------------------------------------------


def test_the_status_surface_carries_the_mask_the_flag_and_the_verdict(client):
    """All three, because presence questions are settled by their combination and the
    investigation that established these facts had none of them available over HTTP."""
    client._handle_ams_data(_ams_push([copy.deepcopy(LOADED_TRAY)], bits="2", power_on=False))
    assert (client.state.ams_tray_exist_bits, client.state.ams_power_on_flag) == ("2", False)
    assert client.state.ams_bits_trusted is True

    zero = _ams_push(copy.deepcopy(PRINTER_1_KEYLESS), bits="0", power_on=True)
    client._handle_ams_data(zero)
    assert (client.state.ams_tray_exist_bits, client.state.ams_power_on_flag) == ("0", True)
    assert client.state.ams_bits_trusted is False, "reported verbatim, believed only once repeated"
