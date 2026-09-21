"""Fleet availability and throughput, classified at READ time.

The observation recorder stores orthogonal facts and ranks none of them; the incident
store stores durable holds. This package is where those become the figures an operator
reads — *how many printers were down, why, for how long, and what came out* — over any
window, in the site's own calendar.

Modules, in the direction they depend:

- ``classifier`` — ``availability_class(seen, open_kinds)``: THE verdict, one function
  for history and for the live tile, with the absence of evidence as an input value.
- ``timeline`` — the pure sweep: rows in, one gapless classified interval list per
  printer out, cut on the site's bucket grid.
- ``projections`` — every series, each a pure fold of that one timeline plus its own
  non-state rows; the identities they keep exact are listed in the module docstring.
- ``loader`` — the only module that reads a database or a live accessor, and the only
  one that is async.

Read-only throughout: nothing here inserts, updates or deletes. That is what lets the
definition of *down* change without invalidating a single stored row.
"""

from backend.app.services.fleet_metrics.classifier import (
    GROUPS,
    NO_SPAN_YET,
    OBSERVATION_GAP,
    AvailabilityClass,
    NoSpanYet,
    ObservationGap,
    Seen,
    availability_class,
    down_fault,
    fault_kind_of,
)
from backend.app.services.fleet_metrics.loader import (
    FleetMetricsError,
    InvalidWindow,
    PrinterUnknown,
    WindowTooLong,
    overview,
    printer_intervals,
    status_now,
)
from backend.app.services.fleet_metrics.projections import (
    ClassTotals,
    EpisodeRow,
    PrintLogRow,
    PrintTally,
    SummaryInputs,
    UnitRow,
    class_totals,
    compose_summary,
    cycle,
    fleet_series,
    matrix,
    print_tally,
    recovery,
    throughput,
    units,
)
from backend.app.services.fleet_metrics.timeline import (
    BASIS_INCIDENTS_ONLY,
    BASIS_OBSERVED,
    BucketHeader,
    FleetTimeline,
    Grid,
    IncidentRow,
    Interval,
    PrinterEvidence,
    PrinterTimeline,
    RosterPrinter,
    SpanRow,
    Window,
    build_timeline,
    build_window,
    printer_slice,
)

__all__ = [
    "BASIS_INCIDENTS_ONLY",
    "BASIS_OBSERVED",
    "GROUPS",
    "NO_SPAN_YET",
    "OBSERVATION_GAP",
    "AvailabilityClass",
    "BucketHeader",
    "ClassTotals",
    "EpisodeRow",
    "FleetMetricsError",
    "FleetTimeline",
    "Grid",
    "IncidentRow",
    "Interval",
    "InvalidWindow",
    "NoSpanYet",
    "ObservationGap",
    "PrintLogRow",
    "PrintTally",
    "PrinterEvidence",
    "PrinterTimeline",
    "PrinterUnknown",
    "RosterPrinter",
    "Seen",
    "SpanRow",
    "SummaryInputs",
    "UnitRow",
    "Window",
    "WindowTooLong",
    "availability_class",
    "build_timeline",
    "build_window",
    "class_totals",
    "compose_summary",
    "cycle",
    "down_fault",
    "fault_kind_of",
    "fleet_series",
    "matrix",
    "overview",
    "print_tally",
    "printer_intervals",
    "printer_slice",
    "recovery",
    "status_now",
    "throughput",
    "units",
]
