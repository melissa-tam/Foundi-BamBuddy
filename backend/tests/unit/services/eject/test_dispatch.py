"""Dispatch-side guards for the (now server-dispatched) eject pipeline.

The eject sweep is a SEPARATE motion-only job — the scheduler no longer injects
any eject block into a print file at dispatch, so an item carrying an
``eject_profile_id`` dispatches its source file UNMODIFIED (apart from the
upstream global per-model start/end snippet, which is unchanged). The eject
end-snippet builder ``build_eject_snippet`` and its scheduler branch were deleted.
"""

import inspect

from backend.app.services import print_scheduler as ps_module
from backend.app.services.eject import dispatch as dispatch_mod


def test_build_eject_snippet_is_deleted():
    """The eject end-snippet builder is gone (its scheduler injection branch too)."""
    assert not hasattr(dispatch_mod, "build_eject_snippet")


def test_dispatch_module_surface_is_motion_only():
    """dispatch.py keeps only the motion-only builder + the cooldown-override
    resolver the eject MONITOR reads; nothing generates an injectable snippet."""
    assert hasattr(dispatch_mod, "build_part_present_eject_file")
    assert hasattr(dispatch_mod, "resolve_cooldown_override")
    # build_part_present_eject_file is motion-only now: no cooldown_temp_c param.
    params = inspect.signature(dispatch_mod.build_part_present_eject_file).parameters
    assert "cooldown_temp_c" not in params


def test_scheduler_has_no_eject_injection_branch():
    """The scheduler's dispatch body no longer builds/superseded an eject block.

    Regression guard for the deleted branch: an ``eject_profile_id`` item flows
    through the SAME global-snippet-only injection path as any other item, so its
    print file is dispatched unmodified (no eject-block supersede, no eject repack).
    Asserted at the source level because driving the full ``_start_print`` would
    exercise many orthogonal farm gates (capability/USB/archive/FTP/MQTT); this
    directly pins the one behaviour under test.
    """
    src = inspect.getsource(ps_module.PrintScheduler._start_print)
    assert "build_eject_snippet" not in src
    assert "eject_snippet" not in src
    # The eject-profile branch that superseded the machine-end snippet is gone;
    # the only injection is the upstream global per-model snippet flow.
    assert "supersede the global end snippet" not in src
    assert "auto-eject block generated from profile" not in src
