"""``usb_storage.usb_present`` — the one decider for "is the USB drive absent?".

The dispatch pre-flight, the run-detail eligibility panel and the fleet
observation recorder all ask this; the knowledge used to be split between the
module's upload-blip set and the scheduler's own ``sdcard is False`` read. Only
a GENUINE absence answers False — an in-flight FTPS upload transiently drops the
H2S ``sdcard`` flag, and that blip must read as unknown on every lane that
registers an upload, the remote-eject lane included.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import usb_storage
from backend.app.services.eject.remote import upload_in_flight as eject_lane_upload_in_flight
from backend.app.services.printer_manager import printer_manager
from backend.app.services.usb_storage import usb_present

PID = 7
OTHER = 8


@pytest.fixture(autouse=True)
def _reset_usb_state():
    usb_storage._reset_state()
    yield
    usb_storage._reset_state()


@pytest.fixture
def statuses(monkeypatch):
    """The house live-status fake: a printer_id -> status map behind get_status."""
    status_map: dict[int, object] = {}
    monkeypatch.setattr(printer_manager, "get_status", lambda pid: status_map.get(pid))
    return status_map


class TestLiveReading:
    def test_present(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=True)
        assert usb_present(PID) is True

    def test_genuinely_absent(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=False)
        assert usb_present(PID) is False

    def test_no_status_is_unknown(self, statuses):
        assert usb_present(PID) is None

    def test_missing_field_is_unknown(self, statuses):
        # The firmware reports sdcard only inside a FULL status report.
        statuses[PID] = SimpleNamespace(state="IDLE")
        assert usb_present(PID) is None

    def test_non_boolean_field_is_unknown(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=None)
        assert usb_present(PID) is None

    def test_disconnected_session_is_unknown(self, statuses):
        # A dropped session's last flag is a memory, not an observation.
        statuses[PID] = SimpleNamespace(sdcard=False, connected=False)
        assert usb_present(PID) is None

    def test_manager_failure_is_unknown(self, monkeypatch):
        def _boom(pid: int):
            raise RuntimeError("manager down")

        monkeypatch.setattr(printer_manager, "get_status", _boom)
        assert usb_present(PID) is None


@pytest.mark.asyncio
class TestUploadBlipSuppression:
    async def test_in_flight_upload_masks_a_false_reading(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=False)
        async with usb_storage.upload_in_flight(PID):
            assert usb_present(PID) is None
        # The blip window closes with the upload.
        assert usb_present(PID) is False

    async def test_in_flight_upload_does_not_mask_a_present_drive(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=True)
        async with usb_storage.upload_in_flight(PID):
            assert usb_present(PID) is True

    async def test_in_flight_on_another_printer_does_not_mask(self, statuses):
        statuses[PID] = SimpleNamespace(sdcard=False)
        async with usb_storage.upload_in_flight(OTHER):
            assert usb_present(PID) is False

    async def test_remote_eject_lane_registration_masks(self, statuses):
        # The eject lane wraps its FTPS upload in the SAME context manager, so its
        # blips are suppressed without that module knowing anything about USB state.
        statuses[PID] = SimpleNamespace(sdcard=False)
        async with eject_lane_upload_in_flight(PID):
            assert usb_present(PID) is None
