/**
 * SettingNumber — the ONE implementation of the Settings page's clamped
 * number field: a `<label htmlFor>` caption, an `InfoHint` carrying the
 * mechanism copy, and a `type="number"` input bounded to [min, max].
 *
 * The rule it owns: **an emptied field is a keystroke, not a value.** A
 * controlled number input written as
 * `value={stored} onChange={write(clamp(parseInt(raw) || fallback))}` cannot be
 * typed into once it is cleared — `parseInt('')` is NaN, the `|| fallback`
 * substitutes a number, and the controlled value immediately re-renders it into
 * the box. With `min={1}` clearing the field and typing `5` yields "15"; with a
 * signed range a lone "-" can never be entered at all. So an unparseable entry
 * ("", "-", "+") is held in a local draft string and writes NOTHING, a
 * parseable one is clamped and written at once, and blur drops the draft back
 * to the stored value. The draft is deliberately local to this component: it is
 * transient input state, never a second home for the setting.
 *
 * `kind` states what the setting stores. `integer` (the default — the two fan
 * speeds, the chamber sustain speed, the plate-hold part top, the idle park
 * depth, the blow-off time) parses with `parseInt`; `step` only tunes the
 * spinner. `decimal` (the cooldown margin) parses with `parseFloat`, and there
 * an entry BELOW `min` is also a keystroke: "0" is how "0.5" begins, so
 * clamping it at once would rewrite the box mid-number. It is held as a draft
 * and clamped when the field is left. Above `max` still clamps at once — more
 * digits can only raise it. The cooling epsilon and the plateau margin keep
 * their own inputs in the monitoring grid.
 *
 * Supplementary copy goes in `hint` and is rendered only in the tooltip
 * (react-best-practices §9); there is no inline help slot. `enabled={false}`
 * reproduces the page's dependent-field treatment — the input is `disabled` and
 * the wrapper dims — for a number that a switch above it has made meaningless.
 *
 * Settings-scoped on purpose, not `components/ui/`: `ui/Field` deliberately
 * does not carry the settings input style.
 */
import { useState } from 'react';
import { InfoHint } from '../ui/InfoHint';

/** What the setting stores; decides the parser and the below-min rule. */
export type SettingNumberKind = 'integer' | 'decimal';

interface SettingNumberProps {
  /** DOM id; ties the visible `<label>` to the input. */
  id: string;
  /** The visible caption — the control's one label. */
  label: string;
  /** Mechanism/consequence copy, rendered only in the InfoHint tooltip. */
  hint: string;
  /** The stored setting. Shown whenever no draft keystroke is pending. */
  value: number;
  /** Called with a clamped value; never called for an unparseable entry. */
  onChange: (value: number) => void;
  min: number;
  max: number;
  /** Spinner increment (and the browser's step validity). */
  step?: number;
  /** `integer` (default) or `decimal`. */
  kind?: SettingNumberKind;
  /** False when a switch above has made this number meaningless. */
  enabled?: boolean;
}

export function SettingNumber({
  id,
  label,
  hint,
  value,
  onChange,
  min,
  max,
  step = 1,
  kind = 'integer',
  enabled = true,
}: SettingNumberProps) {
  const [draft, setDraft] = useState<string | null>(null);
  const parse = (raw: string): number => (kind === 'decimal' ? parseFloat(raw) : parseInt(raw, 10));

  return (
    <div className={`sm:max-w-xs ${enabled ? '' : 'opacity-50'}`}>
      <div className="flex items-center gap-1.5 mb-1">
        <label htmlFor={id} className="block text-xs text-bambu-gray">
          {label}
        </label>
        <InfoHint text={hint} />
      </div>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        step={step}
        value={draft ?? String(value)}
        onChange={(e) => {
          const raw = e.target.value;
          const parsed = parse(raw);
          if (Number.isNaN(parsed) || (kind === 'decimal' && parsed < min)) {
            // Mid-typing ("-", an emptied field, or a decimal's leading "0"):
            // show the raw string, leave the stored value alone.
            setDraft(raw);
            return;
          }
          const clamped = Math.max(min, Math.min(max, parsed));
          // A clamped entry drops the draft so the bound shows at once.
          setDraft(clamped === parsed ? raw : null);
          onChange(clamped);
        }}
        onBlur={() => {
          // A decimal left below its floor commits the floor; any other draft
          // is either unparseable (nothing to commit) or already written.
          const pending = draft === null ? Number.NaN : parse(draft);
          if (!Number.isNaN(pending) && pending < min) onChange(min);
          setDraft(null);
        }}
        disabled={!enabled}
        className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
      />
    </div>
  );
}
