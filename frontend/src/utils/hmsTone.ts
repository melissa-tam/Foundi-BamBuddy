// Honest tone for a printer's HMS error set, decoupled from the raw severity
// number the firmware reports. A `print_error`-sourced fault is hardcoded to
// severity 3 on the backend, so severity alone can never turn a fatal, print-
// ending error red. `hmsTone` folds the gcode_state in: a FAILED print with any
// error is red regardless of severity (H2), while a fatal/serious code (severity
// <= 2) is red in any state. Anything else with errors present is a warning.
import type { HMSError } from '../api/client';

export type HMSTone = 'error' | 'warning' | 'ok';

/**
 * Classify a printer's active HMS errors into a display tone.
 *
 * @param errors    the printer's raw hms_errors (unfiltered — unknown codes count)
 * @param gcodeState the printer's gcode_state (`status.state`), e.g. 'FAILED'
 */
export function hmsTone(
  errors: HMSError[] | null | undefined,
  gcodeState: string | null | undefined,
): HMSTone {
  if (!errors || errors.length === 0) return 'ok';
  const anySevere = errors.some((e) => e.severity <= 2);
  if (anySevere || gcodeState === 'FAILED') return 'error';
  return 'warning';
}
