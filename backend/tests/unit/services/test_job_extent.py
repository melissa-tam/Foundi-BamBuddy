"""The terminal payload's extent keys, read once (``services/job_extent``).

Both carriers of a terminal — the plate gate's ``DepositEvidence`` and the charge's ``JobEvidence`` —
are built on this one parse, and every gram consumer reads its ``charged_extent``: how far a PARTIAL
charge runs, ending at the first layer the job printed with nothing fed (011-H2S / 014-H2S,
2026-10-09: resumed onto an empty toolhead, the jobs ran to the end on air).
"""

import pytest

from backend.app.services.job_extent import ChargedExtent, JobExtent, plate_share
from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.usage_tracker import JobEvidence


def _extent(**fields) -> JobExtent:
    return JobExtent(**JobExtent.payload_fields(fields))


class TestThePayloadIsReadOnce:
    _PAYLOAD = {
        "status": "completed",
        "peaks_reliable": True,
        "last_layer_num": 167,
        "last_progress": 100.0,
        "total_layers": 167,
        "first_unfed_layer": 93,
        "tray_change_log": [(0, 0), (1, 93)],
    }
    _EXTENT_FIELDS = ("peaks_reliable", "last_layer_num", "last_progress", "total_layers", "first_unfed_layer")

    def test_both_carriers_read_the_same_extent(self):
        deposit = DepositEvidence.from_terminal_payload(self._PAYLOAD, is_dry_run=False)
        charge = JobEvidence.from_payload(self._PAYLOAD)
        assert [getattr(deposit, name) for name in self._EXTENT_FIELDS] == [
            getattr(charge, name) for name in self._EXTENT_FIELDS
        ]
        assert deposit.charged_extent == charge.charged_extent == ChargedExtent(layer=93, fraction=93 / 167)

    @pytest.mark.parametrize("value", [None, "93", True, -1, 93.0])
    def test_anything_but_a_layer_number_measured_nothing(self, value):
        assert _extent(first_unfed_layer=value).first_unfed_layer is None

    def test_an_absent_key_is_not_observed(self):
        assert JobExtent(**JobExtent.payload_fields({})) == JobExtent()


class TestChargedExtent:
    def test_nothing_unfed_runs_to_the_jobs_peaks(self):
        """Unchanged: the last layer and the last percent."""
        assert _extent(last_layer_num=50, last_progress=40.0, total_layers=100).charged_extent == ChargedExtent(
            layer=50, fraction=0.4
        )

    def test_an_unfed_layer_ends_the_charge_at_its_share_of_the_plate(self):
        extent = _extent(last_layer_num=167, last_progress=100.0, total_layers=167, first_unfed_layer=93)
        assert extent.charged_extent == ChargedExtent(layer=93, fraction=93 / 167)

    def test_the_last_percent_bounds_the_share_too(self):
        """Two readings of how far the job got: the fed share exceeds neither."""
        extent = _extent(last_layer_num=142, last_progress=50.0, total_layers=167, first_unfed_layer=134)
        assert extent.charged_extent == ChargedExtent(layer=134, fraction=0.5)

    def test_an_attach_that_read_no_percent_still_charges_the_measured_layer(self):
        extent = _extent(peaks_reliable=False, total_layers=167, first_unfed_layer=93)
        assert extent.charged_extent == ChargedExtent(layer=93, fraction=93 / 167)

    def test_with_no_layer_count_the_percent_per_layer_reached_scales_it(self):
        extent = _extent(last_layer_num=142, last_progress=85.0, total_layers=0, first_unfed_layer=134)
        assert extent.charged_extent == ChargedExtent(layer=134, fraction=pytest.approx(0.85 * 134 / 142))

    def test_with_nothing_to_scale_by_the_linear_share_is_zero(self):
        """The G-code cumulative still reads the layer; a linear guess could only over-charge."""
        assert _extent(first_unfed_layer=93).charged_extent == ChargedExtent(layer=93, fraction=0.0)

    def test_it_never_runs_past_the_last_layer_read(self):
        extent = _extent(last_layer_num=80, last_progress=48.0, total_layers=167, first_unfed_layer=93)
        assert extent.charged_extent.layer == 80


class TestPrintedUnfed:
    @pytest.mark.parametrize(
        ("fields", "printed_unfed"),
        [
            ({"total_layers": 167, "first_unfed_layer": 93}, True),
            ({"total_layers": 167, "first_unfed_layer": 166}, True),
            ({"total_layers": 167, "first_unfed_layer": 167}, False),
            ({"total_layers": 0, "last_layer_num": 142, "first_unfed_layer": 134}, True),
            ({"total_layers": 0, "last_layer_num": 0, "first_unfed_layer": 9}, False),
            ({"total_layers": 167, "first_unfed_layer": None}, False),
        ],
        ids=[
            "below_the_count",
            "the_layer_before_the_last",
            "the_last_layer",
            "below_the_layer_reached",
            "no_last_layer",
            "unmeasured",
        ],
    )
    def test_only_layers_below_the_last_one_were_printed_on_air(self, fields, printed_unfed):
        """The last layer is the end-of-print retract; with no last layer known nothing proves either."""
        assert _extent(**fields).printed_unfed is printed_unfed


class TestPlateShare:
    """THE scale of the plate's slicer estimate a terminal is charged, read by every estimate-scaling
    reader (the 3MF lane's linear charge, the archive cost's top-up, the completion notification)."""

    _UNFED = ChargedExtent(layer=93, fraction=93 / 167)

    @pytest.mark.parametrize(
        ("charge", "extent", "share"),
        [
            ("full", None, 1.0),
            ("full", _UNFED, 1.0),
            ("partial", _UNFED, 93 / 167),
            ("partial", None, 0.0),
            ("none", _UNFED, 0.0),
        ],
        ids=["full", "full_ignores_an_extent", "partial_is_the_fed_share", "partial_unmeasured", "none"],
    )
    def test_the_basis_and_the_fed_extent_decide_it(self, charge, extent, share):
        assert plate_share(charge, extent) == share
