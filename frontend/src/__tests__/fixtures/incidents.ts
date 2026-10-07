/**
 * Fixtures for the fault-ledger read (`GET /api/v1/incidents`), shaped like the
 * 2026-10-07 dry run: one printer-scope recurring line (011-H2S extruder
 * overload) and one fleet-scope line (foreign objects on the plate).
 */
import type {
  IncidentOutcome,
  IncidentRow,
  IncidentsResponse,
  IncidentSummary,
  RecurringLine,
} from '../../types/incidents';

/** The fleet fixtures' site zone (`fixtures/fleetMetrics.FIXTURE_TZ_NAME`), spelled here so the two fixture modules import in one direction only. */
const FIXTURE_TZ_NAME = 'Pacific/Auckland';

/** Every outcome at zero — the backend's `dict.fromkeys(OUTCOMES, 0)`. */
export const NO_OUTCOMES: Record<IncidentOutcome, number> = {
  recovering: 0,
  held: 0,
  auto_recovered: 0,
  human_resolved: 0,
  resolved_unpaged: 0,
  taken_over: 0,
  transient: 0,
};

export const RECURRING_PRINTER_ID = 11;
export const RECURRING_PRINTER_NAME = '011-H2S';

export const makeIncidentRow = (overrides: Partial<IncidentRow> = {}): IncidentRow => ({
  id: 352,
  printer_id: 10,
  printer_name: '012-H2S',
  job_id: '1656487314',
  item_id: 3193,
  kind: 'jam',
  external: false,
  code: '0700_8010',
  codes: 'mechanical_feed:0700_8010',
  slot_desc: 'AMS A slot 1',
  status: 'resolved',
  outcome: 'auto_recovered',
  resolution_class: 'wire',
  created_at: '2026-09-27T16:05:34',
  // The fixtures' site zone is UTC+12 (`fixtures/fleetMetrics`).
  utc_offset_minutes: 720,
  escalated_at: null,
  resolved_at: '2026-09-27T16:09:43',
  resolve_source: 'driver_self_heal',
  held_s: 248.8,
  printer_messages: [{ short_code: '0700_8010', description: 'The AMS assist motor is overloaded.' }],
  recurring: false,
  ...overrides,
});

export const makeRecurringPrinterLine = (overrides: Partial<RecurringLine> = {}): RecurringLine => ({
  scope: 'printer',
  printer_id: RECURRING_PRINTER_ID,
  printer_name: RECURRING_PRINTER_NAME,
  kind: 'jam',
  code: '0300_801E',
  printer_message: { short_code: '0300_801E', description: 'The extrusion motor is overloaded.' },
  holds: 9,
  days: 4,
  held_s: 2520,
  last_at: '2026-10-06T08:00:00',
  utc_offset_minutes: 720,
  fleet_median: 0,
  printers_affected: 1,
  roster_size: 16,
  worst: [],
  ...overrides,
});

export const makeRecurringFleetLine = (overrides: Partial<RecurringLine> = {}): RecurringLine => ({
  scope: 'fleet',
  printer_id: null,
  printer_name: null,
  kind: 'plate_vision',
  code: '0500_806E',
  printer_message: { short_code: '0500_806E', description: 'Foreign objects detected on the plate.' },
  holds: 74,
  days: 22,
  held_s: 180_000,
  last_at: '2026-10-06T12:00:00',
  utc_offset_minutes: 720,
  fleet_median: 4,
  printers_affected: 13,
  roster_size: 16,
  worst: [
    { printer_id: 1, printer_name: '001-H2S', holds: 11 },
    { printer_id: 9, printer_name: '009-H2S', holds: 10 },
  ],
  ...overrides,
});

export const makeIncidentSummary = (overrides: Partial<IncidentSummary> = {}): IncidentSummary => ({
  total: 55,
  zero_human: 11,
  declared: 2,
  by_outcome: { ...NO_OUTCOMES, auto_recovered: 11, human_resolved: 33, resolved_unpaged: 6, held: 3, recovering: 1, taken_over: 1 },
  by_kind: { jam: { ...NO_OUTCOMES, auto_recovered: 3, human_resolved: 10 } },
  ...overrides,
});

export const makeIncidentsResponse = (overrides: Partial<IncidentsResponse> = {}): IncidentsResponse => ({
  date_from: '2026-08-01',
  date_to: '2026-09-21',
  tz_name: FIXTURE_TZ_NAME,
  total: 120,
  limit: 50,
  offset: 0,
  summary: makeIncidentSummary(),
  recurring: [makeRecurringPrinterLine(), makeRecurringFleetLine()],
  items: [
    makeIncidentRow(),
    makeIncidentRow({
      id: 353,
      printer_id: RECURRING_PRINTER_ID,
      printer_name: RECURRING_PRINTER_NAME,
      code: '0300_801E',
      codes: 'mechanical_feed:0300_801E',
      outcome: 'held',
      status: 'escalated',
      resolved_at: null,
      resolve_source: null,
      escalated_at: '2026-09-28T10:00:00',
      created_at: '2026-09-28T09:58:00',
      printer_messages: [{ short_code: '0300_801E', description: 'The extrusion motor is overloaded.' }],
      recurring: true,
    }),
  ],
  ...overrides,
});
