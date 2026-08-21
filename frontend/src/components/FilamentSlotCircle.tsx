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
 *                slot border (#1322 follow-up): "physical" for a wire-asserted
 *                empty slot, "present" for a spool the firmware says IS seated
 *                (state 10/11) but could not read, "unknown" for a slot whose
 *                presence the wire has not stated either way. Ignored when
 *                isEmpty is false. "present" is the loud one: a solid
 *                warning-tone ring plus a centred "?" glyph carrying an
 *                aria-label + title, because an unread-but-seated roll used to
 *                render indistinguishably from an empty slot (004-H2S).
 *                "unknown" is the quiet one: a DIMMER dashed ring plus its own
 *                title / screen-reader sentence, so "we don't know" never reads
 *                as the positive claim "nothing is in there".
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
 *                Renders a distinct badge (bottom-right).
 *   noBackupSlot - True when AMS Filament Backup is on and this slot has no
 *                firmware backup partner even though a same-filament roll sits
 *                on the same extruder side, excluded by an exact-match colour
 *                or nozzle-temp difference (`amsHelpers.nearMissBackupSlots`;
 *                010-H2S ran dry twice on 161616FF beside a full 000000FF
 *                roll). Renders the last free corner badge (bottom-left) and is
 *                suppressed on empty slots — an empty slot has no filament to
 *                back up. All four badges carry an icon glyph + aria-label +
 *                title (never colour-only).
 */

import { useTranslation } from 'react-i18next';
import { AlertTriangle, AlertCircle, RotateCcw, Unlink } from 'lucide-react';
import type { EmptySlotKind } from '../utils/amsHelpers';

interface FilamentSlotCircleProps {
  trayColor?: string | null;
  trayType?: string | null;
  isEmpty: boolean;
  emptyKind?: EmptySlotKind | null;
  slotNumber: number | string;
  outOfRotation?: boolean;
  ranOut?: boolean;
  spentCore?: boolean;
  noBackupSlot?: boolean;
}

function isLightFilamentColor(hex: string): boolean {
  if (!hex || hex.length < 6) return false;
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6;
}

export function FilamentSlotCircle({ trayColor, trayType, isEmpty, emptyKind, slotNumber, outOfRotation, ranOut, spentCore, noBackupSlot }: FilamentSlotCircleProps) {
  const { t } = useTranslation();
  // Unknown-presence slots get a quieter border than wire-asserted empty so they
  // read as "the printer has not said" rather than "definitely no spool".
  const stateUnknown = isEmpty && emptyKind === 'unknown';
  const emptyBorderColor = stateUnknown ? '#3d3d3d' : '#666';
  // A spool the firmware reports SEATED but could not read (state 10/11). It is
  // not an empty slot and must not look like one: solid warning-tone ring +
  // a "?" glyph in place of the slot number, both carrying the label so the
  // state is never signalled by colour alone.
  const presentUnread = isEmpty && emptyKind === 'present';
  const presentUnreadLabel = t('ams.slotPresentUnread');
  const stateUnknownLabel = t('ams.slotStateUnknown');
  const outOfRotationLabel = t('ams.outOfRotation');
  const ranOutLabel = t('printers.slot.ranOut');
  const spentCoreLabel = t('printers.slot.spentCore');
  const noBackupSlotLabel = t('printers.slot.noBackupSlot');
  // An empty slot has no filament to back up — the badge would be a claim about
  // nothing. The three sibling badges describe a spool, this one describes the
  // filament in the tray, so it is the only one gated on isEmpty.
  const showNoBackupSlot = !!noBackupSlot && !isEmpty;
  return (
    <div
      className={`relative w-3.5 h-3.5 rounded-full mx-auto mb-0.5 border-2 flex items-center justify-center${
        presentUnread ? ' border-amber-600 dark:border-amber-400' : ''
      }`}
      title={presentUnread ? presentUnreadLabel : stateUnknown ? stateUnknownLabel : undefined}
      style={{
        backgroundColor: trayColor ? `#${trayColor}` : (trayType ? '#333' : 'transparent'),
        // The seated-but-unread ring comes from the theme-paired amber classes
        // above (light: amber-600, dark: amber-400 — both clear 3:1 against the
        // slot background); an inline borderColor here would override them.
        ...(presentUnread
          ? {}
          : { borderColor: isEmpty ? emptyBorderColor : 'rgba(255,255,255,0.1)' }),
        borderStyle: isEmpty && !presentUnread ? 'dashed' : 'solid',
      }}
    >
      {presentUnread ? (
        <span
          role="img"
          aria-label={presentUnreadLabel}
          title={presentUnreadLabel}
          className="text-[7px] font-bold leading-none select-none text-amber-600 dark:text-amber-400"
        >
          ?
        </span>
      ) : (
        <span
          className="text-[6px] font-bold leading-none select-none"
          style={{ color: trayColor && isLightFilamentColor(trayColor) ? '#000' : '#fff' }}
        >
          {slotNumber}
        </span>
      )}
      {/* Unknown presence keeps the slot number visible (it is still the slot's
          identity) and carries the state as a screen-reader sentence beside the
          title above — never colour/border alone (WCAG 1.4.1). */}
      {stateUnknown && <span className="sr-only">{stateUnknownLabel}</span>}
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
      {showNoBackupSlot && (
        // "No firmware backup partner" badge — the last free corner
        // (bottom-left). Shares the jam badge's amber tone deliberately: four
        // badges in one 14 px circle need ONE tone system, and the corner plus
        // the Unlink glyph already tell them apart. Not colour-only: the glyph
        // carries the meaning and aria-label + title carry the sentence to
        // screen readers and on hover/focus.
        <span
          role="img"
          aria-label={noBackupSlotLabel}
          title={noBackupSlotLabel}
          className="absolute -bottom-1 -left-1 flex items-center justify-center w-2.5 h-2.5 rounded-full bg-amber-400 ring-1 ring-bambu-dark"
        >
          <Unlink className="w-[7px] h-[7px] text-black" aria-hidden="true" strokeWidth={3} />
        </span>
      )}
    </div>
  );
}
