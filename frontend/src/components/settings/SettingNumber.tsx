/**
 * SettingNumber — the ONE implementation of the Settings page's clamped
 * integer field: a `<label htmlFor>` caption, an `InfoHint` carrying the
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
 * Integer-valued by construction (`parseInt`): every consumer — the two fan
 * speeds, the chamber sustain speed, the plate-hold part top and the idle park
 * depth — stores a whole number. `step` only tunes the spinner increment.
 * Fractional settings (the cooling epsilon, the plateau margin) keep their own
 * inputs in the monitoring grid.
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

interface SettingNumberProps {
  /** DOM id; ties the visible `<label>` to the input. */
  id: string;
  /** The visible caption — the control's one label. */
  label: string;
  /** Mechanism/consequence copy, rendered only in the InfoHint tooltip. */
  hint: string;
  /** The stored setting. Shown whenever no draft keystroke is pending. */
  value: number;
  /** Called with a clamped integer; never called for an unparseable entry. */
  onChange: (value: number) => void;
  min: number;
  max: number;
  /** Spinner increment only — the stored value is always an integer. */
  step?: number;
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
  enabled = true,
}: SettingNumberProps) {
  const [draft, setDraft] = useState<string | null>(null);

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
          const parsed = parseInt(raw, 10);
          if (Number.isNaN(parsed)) {
            // Mid-typing ("-", or an emptied field): show the raw string,
            // leave the stored value alone.
            setDraft(raw);
            return;
          }
          const clamped = Math.max(min, Math.min(max, parsed));
          // A clamped entry drops the draft so the bound shows at once.
          setDraft(clamped === parsed ? raw : null);
          onChange(clamped);
        }}
        onBlur={() => setDraft(null)}
        disabled={!enabled}
        className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green"
      />
    </div>
  );
}
