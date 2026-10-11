"""THE scripted toolhead FEED STATE for tests whose printer is a scripted ``PrinterState``.

Production has ONE stepper of a ``feed_state.FeedTracker``: ``BambuMQTTClient._step_feed``, once per
status push, after every field of the push is applied (``test_code_quality.TestFeedStateOwnership``).
A consumer test that drives a REAL client (``client._process_message``) reads that client's
``feed()`` and needs nothing here. A test whose printer is a scripted state — the recovery driver's
``FakeClient``, the rule table's contexts, the status projection — gets its reading from THIS
helper: a real tracker, stepped with the frame the client would build off that state
(``FeedFrame.of``), one step per push. Never a hand-built ``FeedReading``: every phase a consumer
test reads is one the tracker itself reached.

The frame mirrors ``_step_feed`` field for field: ``live`` is ``live_reading.reads_live(state)``;
the posture is ``bambu_mqtt.ams_mid_filament_change(state)`` and every scripted push CARRIES it (a
scripted state always holds ``ams_status_main``); the layer is ``state.layer_num`` (the scripted
job has no stale predecessor); the job-lifecycle gate is a job state (RUNNING / PAUSE / PREPARE …,
never IDLE / FINISH / FAILED); ``farm_acting`` is the provider's answer at the push.
"""

from __future__ import annotations

from collections.abc import Callable

from backend.app.services.bambu_mqtt import ams_mid_filament_change
from backend.app.services.feed_state import FeedFrame, FeedReading, FeedTracker, JobBoundary
from backend.app.services.live_reading import reads_live

# The gcode states in which the scripted printer holds NO job (the client's lifecycle gate shut).
_NO_JOB_STATES = frozenset({"", "IDLE", "FINISH", "FAILED", "UNKNOWN"})


def scripted_frame(
    state: object,
    *,
    farm_acting: bool = False,
    boundary: JobBoundary = JobBoundary.NONE,
) -> FeedFrame:
    """The frame ``BambuMQTTClient._step_feed`` would build off this scripted state."""
    gcode = (getattr(state, "state", None) or "").upper()
    return FeedFrame.of(
        state,
        live=reads_live(state),  # type: ignore[arg-type]
        posture_carried=True,
        posture=ams_mid_filament_change(state),
        layer=int(getattr(state, "layer_num", 0) or 0),
        job_active=gcode not in _NO_JOB_STATES,
        farm_acting=farm_acting,
        boundary=boundary,
    )


class ScriptedFeed:
    """One scripted printer's feed state: a real tracker, stepped once per :meth:`push`."""

    def __init__(self, *, farm_acting: Callable[[], bool] | None = None) -> None:
        self._tracker = FeedTracker()
        self._farm_acting = farm_acting or (lambda: False)

    @property
    def reading(self) -> FeedReading:
        """The latest reading (UNKNOWN before the first push) — what ``client.feed()`` returns."""
        return self._tracker.reading

    def push(self, state: object, *, boundary: JobBoundary = JobBoundary.NONE) -> FeedReading:
        """ONE status push of ``state``: step the tracker and return the new reading."""
        return self._tracker.step(scripted_frame(state, farm_acting=self._farm_acting(), boundary=boundary))

    def settle(self, state: object) -> FeedReading:
        """A printer this test has watched report: its session's first push re-seeds (no event),
        the next is read — the reading a client holds once it has seen the printer report."""
        self.push(state)
        return self.push(state)
