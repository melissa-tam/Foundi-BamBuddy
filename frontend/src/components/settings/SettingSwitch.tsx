/**
 * SettingSwitch — the ONE implementation of the Settings page's on/off row:
 * a visible caption, an `InfoHint` carrying the mechanism copy, and the
 * sr-only checkbox rendered as the farm's pill switch.
 *
 * The rule it owns: **one label per control** (react-best-practices §9). The
 * caption text is also the checkbox's `aria-label`, so the switch has exactly
 * one accessible name and a test can address it as
 * `getByRole('checkbox', { name: label })`. Supplementary copy — what the
 * switch causes, what happens when it is off — belongs in `hint` and is
 * rendered only in the tooltip; this component deliberately has no slot for an
 * inline `<p>` help line under the row.
 *
 * Extracted from the four hand-rolled copies the Settings page carried (the
 * cooldown plate hold, the idle bed park, and now the two cooldown fans); the
 * markup is byte-for-byte what the plate-hold row rendered, so the visuals did
 * not change when the copies were deleted.
 */
import { InfoHint } from '../ui/InfoHint';

interface SettingSwitchProps {
  /** The visible caption AND the checkbox's accessible name — one label. */
  label: string;
  /** Mechanism/consequence copy, rendered only in the InfoHint tooltip. */
  hint: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** Optional DOM id for the checkbox (callers rarely need one). */
  id?: string;
}

export function SettingSwitch({ label, hint, checked, onChange, id }: SettingSwitchProps) {
  return (
    <div className="flex items-center justify-between pt-1">
      <div className="flex items-center gap-1.5 flex-1 mr-4">
        <p className="text-sm text-white">{label}</p>
        <InfoHint text={hint} />
      </div>
      <label className="relative inline-flex items-center cursor-pointer">
        <input
          id={id}
          type="checkbox"
          aria-label={label}
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="sr-only peer"
        />
        <div className="w-11 h-6 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-bambu-green"></div>
      </label>
    </div>
  );
}
