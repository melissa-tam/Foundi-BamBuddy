/**
 * useTabs — the selection + identity half of the app's one tablist primitive
 * (`components/ui/Tabs.tsx` renders it: `TabList` for the strip, `TabPanel`
 * for the panel).
 *
 * The hook exists for one reason: the tab and its panel must agree on two ids
 * (`aria-controls` one way, `aria-labelledby` the other), and a call site that
 * spells those ids out can mistype one and produce a strip that looks right
 * and announces nothing. Minting them from a single `useId` and handing BOTH
 * components the same model makes that drift unrepresentable.
 *
 * Selection itself stays where each site already keeps it — a `useState`, a
 * URL search param, a localStorage-backed state — so the primitive is
 * CONTROLLED and owns focus, never selection.
 *
 * Lives in its own module, not beside the components, so `Tabs.tsx` stays
 * component-only for `react-refresh/only-export-components` (same reason as
 * `components/filamentSwatchHelpers.ts`).
 */
import { useId } from 'react';
import type { ReactNode } from 'react';
import type { LucideIcon } from 'lucide-react';

/** One tab. `label` is the tab's accessible name. */
export interface TabDefinition<Id extends string> {
  id: Id;
  label: string;
  /** Optional leading icon, rendered `w-4 h-4` and hidden from the a11y tree. */
  icon?: LucideIcon;
  /**
   * Optional trailing adornment — a count pill or a liveness dot. Supplied as
   * a node because the existing strips style it from the selected state, which
   * the call site already knows.
   */
  badge?: ReactNode;
  /** Focus skips a disabled tab and it cannot be selected. */
  disabled?: boolean;
}

/** The identity + selection shared by a `TabList` and its `TabPanel`. */
export interface TabsModel<Id extends string> {
  items: TabDefinition<Id>[];
  value: Id;
  onChange: (id: Id) => void;
  tabId: (id: Id) => string;
  panelId: (id: Id) => string;
}

export interface UseTabsOptions<Id extends string> {
  /** Currently selected tab id. */
  value: Id;
  onChange: (id: Id) => void;
  items: TabDefinition<Id>[];
}

export function useTabs<Id extends string>({ value, onChange, items }: UseTabsOptions<Id>): TabsModel<Id> {
  const baseId = useId();
  return {
    items,
    value,
    onChange,
    tabId: (id) => `${baseId}tab-${id}`,
    panelId: (id) => `${baseId}panel-${id}`,
  };
}
