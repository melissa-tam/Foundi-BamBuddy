// Display formatting for an HMS fault's canonical hex identifier.
//
// The firmware identifier is 16 hex chars for hms[]-array faults (four 4-char
// groups: module-part-code-... ) and 8 hex chars for print_error faults (two
// groups). We render it grouped with hyphens so the operator sees the FULL
// code. The lossy short code ("MMMM_CCCC") only ever kept the first and last
// group, silently hiding the two middle groups of a 16-char fault — see #1830.
//
// Falls back to the short code (its single "_" rendered as "-") when full_code
// is absent or malformed, and to "" when neither is usable.
const HEX_16 = /^[0-9a-fA-F]{16}$/;
const HEX_8 = /^[0-9a-fA-F]{8}$/;

/**
 * Format an HMS code for display.
 *
 * @param fullCode  the firmware-matching hex identifier (8 or 16 hex chars)
 * @param shortCode the canonical "MMMM_CCCC" short code (fallback)
 * @returns uppercase, hyphen-grouped code, or "" when nothing is usable
 */
export function formatHmsCode(fullCode?: string, shortCode?: string): string {
  if (fullCode && HEX_16.test(fullCode)) {
    const c = fullCode.toUpperCase();
    return `${c.slice(0, 4)}-${c.slice(4, 8)}-${c.slice(8, 12)}-${c.slice(12, 16)}`;
  }
  if (fullCode && HEX_8.test(fullCode)) {
    const c = fullCode.toUpperCase();
    return `${c.slice(0, 4)}-${c.slice(4, 8)}`;
  }
  return shortCode ? shortCode.replace('_', '-').toUpperCase() : '';
}
