"""Regression tests for the failure CATEGORY a terminal records (archive / print log).

The category is one fact of the terminal's one classification
(``terminal_outcome.build_terminal_outcome`` → ``TerminalOutcome.failure_category``); it
used to be ``main.derive_failure_reason``. Two contracts are pinned here:

* user-cancelled prints are never archived as "Layer shift" — the bug seen on H2D where
  the firmware's cancel-sequence module-0x0C HMS was matched by the old broad heuristic
  (``module == 0x0C → Layer shift``);
* (CHANGED 2026-09-24) the category comes from the PRINTER's evidence for any terminal
  that ended without its part, and "User cancelled" only for a stop a human attributed
  (the UI mark or the screen echo). A ``cancelled`` nobody attributed — a no-deposit stop,
  the reconcile's unknown outcome — is no longer labelled "User cancelled": nobody said so.
"""

from __future__ import annotations

import pytest

from backend.app.services.plate_occupancy import DepositEvidence
from backend.app.services.terminal_outcome import build_terminal_outcome


def _category(status: str, hms: list[dict] | None, *, verdict: str | None = None) -> str | None:
    """The category a terminal with this printer word and HMS list records."""
    return build_terminal_outcome(
        raw_status=status,
        verdict=verdict,
        open_incidents=(),
        job_id="job-1",
        evidence=DepositEvidence(
            final_status=status, is_dry_run=False, peaks_reliable=True, last_layer_num=10, last_progress=40.0
        ),
        first_article=False,
        is_eject=False,
        hms_errors=hms,
    ).failure_category


# ---------------------------------------------------------------------------
# Attribution-based reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["operator_ui", "operator_screen"])
@pytest.mark.parametrize("status", ["aborted", "cancelled", "failed"])
def test_an_operator_stop_the_printer_explained_nothing_about_is_user_cancelled(status: str, verdict: str) -> None:
    assert _category(status, None, verdict=verdict) == "User cancelled"
    assert _category(status, [], verdict=verdict) == "User cancelled"


@pytest.mark.parametrize("status", ["aborted", "cancelled"])
def test_an_unattributed_cancel_is_not_labelled_user_cancelled(status: str) -> None:
    """CHANGED contract: nobody pressed Stop, so nothing says a user cancelled it."""
    assert _category(status, None) is None
    assert _category(status, None, verdict="reconcile_unknown") is None


def test_the_printers_category_outranks_the_operator_attribution() -> None:
    """A human stopped a print the printer had already explained — the printer's word is the category."""
    runout = [{"code": "0x8011", "attr": 0x07FF_0000, "module": 0x07, "severity": 2}]
    assert _category("failed", runout, verdict="operator_ui") == "Filament runout"


def test_completed_status_returns_none() -> None:
    assert _category("completed", None) is None
    layer_shift = [{"code": "0x4057", "attr": 0x0300_0000, "module": 0x03, "severity": 1}]
    assert _category("completed", layer_shift) is None


# ---------------------------------------------------------------------------
# H2D regression: cancel-sequence HMS must not be labelled "Layer shift"
# ---------------------------------------------------------------------------


def test_h2d_cancel_module_0x0c_is_not_layer_shift() -> None:
    """0C00_001B is the H2D cancel-sequence echo, not a real layer-shift code.

    The old `module == 0x0C → Layer shift` heuristic mislabeled every user-cancel
    on H2D as a layer-shift failure. This pins that code to None.
    """
    h2d_cancel_hms = [
        {"code": "0x2001b", "attr": 0x0C000C00, "module": 0x0C, "severity": 1},
        {"code": "0x400c", "attr": 0x03002C0C, "module": 0x03, "severity": 3},
    ]
    assert _category("failed", h2d_cancel_hms) is None


def test_unknown_module_0x0c_code_returns_none() -> None:
    """Any module-0x0C code we don't have an explicit short-code mapping for must
    leave failure_reason=None — being honest beats guessing."""
    unknown_hms = [{"code": "0x4099", "attr": 0x0C00_0000, "module": 0x0C, "severity": 2}]
    assert _category("failed", unknown_hms) is None


# ---------------------------------------------------------------------------
# Genuine failure modes still classified correctly
# ---------------------------------------------------------------------------


def test_real_layer_shift_short_code_detected() -> None:
    """0300_4057 ("Z-axis step loss") is a real layer-shift code from the wiki."""
    hms = [{"code": "0x4057", "attr": 0x0300_0000, "module": 0x03, "severity": 1}]
    assert _category("failed", hms) == "Layer shift"


def test_real_filament_runout_short_code_detected() -> None:
    """07FF_8011 = external filament runout."""
    hms = [{"code": "0x8011", "attr": 0x07FF_0000, "module": 0x07, "severity": 2}]
    assert _category("failed", hms) == "Filament runout"


def test_real_clogged_nozzle_short_code_detected() -> None:
    """0300_4006 = "The nozzle is clogged"."""
    hms = [{"code": "0x4006", "attr": 0x0300_0000, "module": 0x03, "severity": 1}]
    assert _category("failed", hms) == "Clogged nozzle"


def test_a_stopped_print_carries_the_printers_category_too() -> None:
    """CHANGED contract: any terminal that ended without its part, not only ``failed``."""
    hms = [{"code": "0x4006", "attr": 0x0300_0000, "module": 0x03, "severity": 1}]
    assert _category("aborted", hms) == "Clogged nozzle"


def test_first_matching_code_wins() -> None:
    """When multiple known codes are present, the first one in the list wins."""
    hms = [
        {"code": "0x4057", "attr": 0x0300_0000, "module": 0x03, "severity": 1},  # layer shift
        {"code": "0x8011", "attr": 0x07FF_0000, "module": 0x07, "severity": 2},  # filament runout
    ]
    assert _category("failed", hms) == "Layer shift"


def test_failed_with_no_hms_returns_none() -> None:
    assert _category("failed", None) is None
    assert _category("failed", []) is None


# ---------------------------------------------------------------------------
# Code-format tolerance (MQTT may send int or hex string)
# ---------------------------------------------------------------------------


def test_int_code_field_accepted() -> None:
    """The MQTT parser sometimes leaves `code` as an int rather than a hex string."""
    hms = [{"code": 0x4057, "attr": 0x0300_0000, "module": 0x03, "severity": 1}]
    assert _category("failed", hms) == "Layer shift"
