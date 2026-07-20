/**
 * FilamentSlotCircle renders a small color circle with the 1-based slot
 * number centered inside, matching the style used on AMS cards in PrintersPage.
 *
 * Props:
 *   trayColor  - 6-char hex color string WITHOUT leading '#' (e.g. "FF0000").
 *                Pass undefined / empty string when the slot is empty.
 *   trayType   - Filament material string (e.g. "PLA").  Used to decide the
 *                fallback background when there is no color but a type is known.
 *   isEmpty    - Whether the slot contains no filament.
 *   emptyKind  - Optional refinement of the empty state used to render the
 *                slot border (#1322 follow-up): "physical" for firmware-
 *                confirmed no spool (state 9/10), "reset" for slots where
 *                the user cleared the assignment but the firmware hasn't
 *                positively confirmed emptiness. Ignored when isEmpty is false.
 *   slotNumber - 1-based slot number to display inside the circle. Accepts
 *                a string for non-numeric labels (e.g. "L" / "R" for the
 *                dual-nozzle external trays, where carrying a separate
 *                Ext-L/Ext-R caption underneath made the row taller).
 *   outOfRotation - True when a spool jam took this spool out of rotation
 *                (Spool.feed_fault_at != null; #feed-fault). Renders a small
 *                amber warning badge (top-right) — NOT colour-only: an icon glyph
 *                plus an aria-label + title carry the meaning for screen readers
 *                and on hover/focus.
 *   ranOut     - True when a live filament-runout HMS names THIS AMS slot as the
 *                exhausted one (W6). Renders a distinct red badge (top-left) so
 *                the operator can see remotely which slot to refill during a
 *                runout PAUSE (when the green active ring has cleared).
 *   spentCore  - True when the assigned spool is hardware-certain spent
 *                (Spool.spent_at != null; W6) — the core needs replacing.
 *                Renders a distinct badge (bottom-right). All three badges carry
 *                an icon glyph + aria-label + title (never colour-only).
 */

import { useTranslation } from 'react-i18next';
import { AlertTriangle, AlertCircle, RotateCcw } from 'lucide-react';

interface FilamentSlotCircleProps {
  trayColor?: string | null;
  trayType?: string | null;
  isEmpty: boolean;
  emptyKind?: 'physical' | 'reset' | null;
  slotNumber: number | string;
  outOfRotation?: boolean;
  ranOut?: boolean;
  spentCore?: boolean;
}

function isLightFilamentColor(hex: string): boolean {
  if (!hex || hex.length < 6) return false;
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6;
}

export function FilamentSlotCircle({ trayColor, trayType, isEmpty, emptyKind, slotNumber, outOfRotation, ranOut, spentCore }: FilamentSlotCircleProps) {
  const { t } = useTranslation();
  // Reset slots get a quieter border than physical-empty so they read as
  // "cleared but possibly still has a spool the firmware hasn't confirmed
  // gone" rather than "definitely no spool".
  const emptyBorderColor = emptyKind === 'reset' ? '#3d3d3d' : '#666';
  const outOfRotationLabel = t('ams.outOfRotation');
  const ranOutLabel = t('printers.slot.ranOut');
  const spentCoreLabel = t('printers.slot.spentCore');
  return (
    <div
      className="relative w-3.5 h-3.5 rounded-full mx-auto mb-0.5 border-2 flex items-center justify-center"
      style={{
        backgroundColor: trayColor ? `#${trayColor}` : (trayType ? '#333' : 'transparent'),
        borderColor: isEmpty ? emptyBorderColor : 'rgba(255,255,255,0.1)',
        borderStyle: isEmpty ? 'dashed' : 'solid',
      }}
    >
      <span
        className="text-[6px] font-bold leading-none select-none"
        style={{ color: trayColor && isLightFilamentColor(trayColor) ? '#000' : '#fff' }}
      >
        {slotNumber}
      </span>
      {outOfRotation && (
        // Corner warning badge. Not colour-only: an AlertTriangle glyph carries
        // the meaning; aria-label + title expose the tooltip text to screen
        // readers and on hover/focus (the title attr is keyboard-discoverable).
        <span
          role="img"
          aria-label={outOfRotationLabel}
          title={outOfRotationLabel}
          className="absolute -top-1 -right-1 flex items-center justify-center w-2.5 h-2.5 rounded-full bg-amber-400 ring-1 ring-bambu-dark"
        >
          <AlertTriangle className="w-[7px] h-[7px] text-black" aria-hidden="true" strokeWidth={3} />
        </span>
      )}
      {ranOut && (
        // Distinct red "ran out" badge (top-left, opposite the amber jam badge so
        // both read at once). Not colour-only: an AlertCircle glyph + aria-label
        // + title carry the meaning.
        <span
          role="img"
          aria-label={ranOutLabel}
          title={ranOutLabel}
          className="absolute -top-1 -left-1 flex items-center justify-center w-2.5 h-2.5 rounded-full bg-red-500 ring-1 ring-bambu-dark"
        >
          <AlertCircle className="w-[7px] h-[7px] text-white" aria-hidden="true" strokeWidth={3} />
        </span>
      )}
      {spentCore && (
        // Distinct "spent core — replace roll" badge (bottom-right). Not
        // colour-only: a RotateCcw glyph + aria-label + title carry the meaning.
        <span
          role="img"
          aria-label={spentCoreLabel}
          title={spentCoreLabel}
          className="absolute -bottom-1 -right-1 flex items-center justify-center w-2.5 h-2.5 rounded-full bg-purple-500 ring-1 ring-bambu-dark"
        >
          <RotateCcw className="w-[7px] h-[7px] text-white" aria-hidden="true" strokeWidth={3} />
        </span>
      )}
    </div>
  );
}
