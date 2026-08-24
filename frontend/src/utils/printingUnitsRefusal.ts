import { ApiError } from '../api/client';

/**
 * Printer names carried by a `*_has_printing_units` 409 refusal, or null for
 * any other failure.
 *
 * The backend refuses a destructive delete that would reach rows a live print
 * still depends on, and names the printers in a structured detail. Two callers
 * raise that refusal from different resources, so the expected `code` is a
 * parameter:
 *   - `run_has_printing_units`   deleting a production run (ProductionRunsPage)
 *   - `user_has_printing_units`  deleting a user's items (SettingsPage)
 *
 * Keyed off the stable `code`, never off the message: the backend's sentence is
 * only an English fallback for non-UI clients, so matching on it would break the
 * moment anyone rewords it and would silently show English in every other
 * locale. The names are what the operator can act on — a row id names nothing
 * they can walk up to.
 *
 * Returns null rather than an empty array for "no printers to name", so callers
 * get one falsy check for both "different failure" and "refusal without usable
 * names" — neither can render the operator a useful sentence.
 */
export function printingUnitPrinters(error: Error | null, code: string): string[] | null {
  if (!(error instanceof ApiError) || error.code !== code) return null;
  const raw: unknown = error.detail?.printers;
  if (!Array.isArray(raw)) return null;
  const entries: unknown[] = raw;
  const names = entries.filter((entry): entry is string => typeof entry === 'string');
  return names.length > 0 ? names : null;
}
