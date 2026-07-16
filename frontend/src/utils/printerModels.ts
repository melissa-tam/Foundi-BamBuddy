/**
 * Shared SSDP-model-code → display-name map.
 *
 * Single source of truth for turning the device codes reported over SSDP
 * (e.g. "O1S", "C11") into the human display names used across the UI
 * (e.g. "H2S", "P1P"). Previously duplicated inline in PrintersPage and
 * SpoolBuddyAmsPage; consolidated here so the two never drift again.
 *
 * The P-series SSDP codes were WRONG in the old copies (C11→P1S, C12→P1P,
 * C13→P2S, N7 missing). Corrected here to match the backend's authoritative
 * VP-code fix map (backend/app/core/database.py `vp_model_fixes`): the display
 * names P1P/P1S/X1E/P2S map to the SSDP codes C11/C12/C13/N7 respectively.
 */

// SSDP device code → display name. Direct-match entries let an already-display
// value pass through unchanged.
const MODEL_CODE_MAP: Record<string, string> = {
  // H2 Series
  O1D: 'H2D',
  O1E: 'H2D Pro',
  O2D: 'H2D Pro',
  O1C: 'H2C',
  O1C2: 'H2C',
  O1S: 'H2S',
  // X1 Series
  'BL-P001': 'X1C',
  'BL-P002': 'X1',
  'BL-P003': 'X1E',
  // X2 Series
  N6: 'X2D',
  // A2 Series
  N9: 'A2L',
  // P Series (SSDP device codes — corrected to match backend vp_model_fixes:
  // C11=P1P, C12=P1S, C13=X1E, N7=P2S)
  C11: 'P1P',
  C12: 'P1S',
  C13: 'X1E',
  N7: 'P2S',
  // A1 Series
  N2S: 'A1',
  N1: 'A1 Mini',
  // Direct matches (already-display values pass through)
  X1C: 'X1C',
  X1: 'X1',
  X1E: 'X1E',
  X2D: 'X2D',
  P1S: 'P1S',
  P1P: 'P1P',
  P2S: 'P2S',
  A1: 'A1',
  'A1 Mini': 'A1 Mini',
  A2L: 'A2L',
  H2D: 'H2D',
  'H2D Pro': 'H2D Pro',
  H2C: 'H2C',
  H2S: 'H2S',
};

/** Map an SSDP model code to its display name; unknown codes pass through. */
export function mapModelCode(ssdpModel: string | null): string {
  if (!ssdpModel) return '';
  return MODEL_CODE_MAP[ssdpModel] || ssdpModel;
}
