/**
 * Tabs — the app's ONE tablist implementation (WAI-ARIA Tabs pattern).
 *
 * A strip of plain buttons carries no `role`, no panel association and no
 * keyboard model: a keyboard user Tab-stops through every section to reach the
 * panel, and a screen reader hears "button" with no "selected", no "3 of 13"
 * and no link to the content it swaps. New tab strips use this primitive.
 *
 * Three pieces, deliberately separate:
 *
 *   - `hooks/useTabs` mints the id pair (`useId`) and carries selection.
 *     Passing the SAME model object to the list and the panel is what
 *     guarantees `aria-controls` / `aria-labelledby` can never drift apart.
 *   - `TabList` renders the strip and owns focus management only.
 *   - `TabPanel` renders the active panel's container.
 *
 * Selection state stays where it already lives at each site (a `useState`, a
 * URL search param, localStorage): the primitive is CONTROLLED (`value` +
 * `onChange`) and owns focus, never selection.
 *
 * Keyboard model (APG "tabs with automatic activation"): the strip is ONE tab
 * stop — the selected tab is `tabIndex=0`, every other tab `-1` — and arrow
 * keys move focus with wrap-around, Home/End jump to the ends, disabled tabs
 * are skipped, and selection FOLLOWS focus. Automatic activation is the right
 * choice here because every panel in this app renders instantly from data the
 * page already holds; nothing is fetched by arrowing across the strip.
 *
 * Disabled tabs are `aria-disabled`, never natively `disabled` — the same
 * reasoning as `Pager`'s nav buttons, plus one of its own: a disabled tab
 * usually carries a tooltip saying WHY it is unavailable (a fleet-matrix lens
 * before the recorder has data), and a natively disabled button
 * fires no hover or focus events, so that explanation would be unreachable.
 *
 * `aria-controls` is set on the SELECTED tab only. Every site here renders the
 * active panel alone, so the other tabs' panels are not in the DOM; pointing
 * `aria-controls` at an absent id is a dangling IDREF (the APG makes the
 * attribute optional for exactly this case).
 */
import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import type { TabsModel } from '../../hooks/useTabs';

/**
 * The app's three tab idioms, kept as three variants rather than three
 * components (`Button variant`, not `PrimaryButton`/`DangerButton`):
 *
 *   - `underline` — page-level strip, white active label (Queue).
 *   - `pill` — in-card segmented strip on a tinted track (the Local/LDAP
 *     switch in the three create-user dialogs).
 *   - `rail` — the Settings idiom: an underline strip that becomes a vertical
 *     left rail at `lg`, green active label.
 *
 * Each variant owns the TAB treatment and the minimum container it needs. No
 * variant sets spacing, overflow or breakpoint layout on the container — those
 * are the call site's (a sticky rail, `overflow-x-auto`, a bottom margin), so
 * `className` can be appended without two competing utilities fighting over
 * stylesheet order.
 */
export type TabsVariant = 'underline' | 'pill' | 'rail';

interface VariantStyle {
  list: string;
  tab: string;
  selected: string;
  idle: string;
  /** Arrow keys the strip claims. The rail is horizontal below `lg` and
   *  vertical above it, so it answers to both axes. */
  verticalKeys: boolean;
}

const VARIANTS: Record<TabsVariant, VariantStyle> = {
  underline: {
    list: 'flex gap-1 border-b border-bambu-dark-tertiary',
    tab: 'px-4 py-2.5 text-sm flex items-center gap-2 border-b-2 -mb-px transition-colors whitespace-nowrap',
    selected: 'text-white border-bambu-green font-medium',
    idle: 'text-bambu-gray border-transparent hover:text-white',
    verticalKeys: false,
  },
  pill: {
    list: 'flex items-center gap-1 p-1 bg-bambu-dark-secondary rounded-lg',
    tab: 'flex-1 px-3 py-2 text-sm rounded-md transition-colors inline-flex items-center justify-center gap-2',
    selected: 'bg-bambu-green/15 text-bambu-green',
    idle: 'text-bambu-gray hover:text-white',
    verticalKeys: false,
  },
  rail: {
    list: 'flex flex-wrap gap-1 border-b border-bambu-dark-tertiary',
    tab: 'px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px lg:border-b-0 lg:border-l-2 lg:-ml-px lg:mb-0 lg:justify-start flex items-center gap-2',
    selected: 'text-bambu-green border-bambu-green',
    idle: 'text-bambu-gray hover:text-gray-900 dark:hover:text-white border-transparent',
    verticalKeys: true,
  },
};

const FOCUS_RING = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50';

export interface TabListProps<Id extends string> {
  tabs: TabsModel<Id>;
  /**
   * Accessible name of the strip — REQUIRED, because a page with two strips
   * (Settings and its Users sub-tabs) is otherwise two unnamed "tab list"
   * landmarks a screen-reader user cannot tell apart.
   */
  ariaLabel: string;
  variant: TabsVariant;
  /** Appended to the variant's container classes (spacing, overflow, layout). */
  className?: string;
}

export function TabList<Id extends string>({ tabs, ariaLabel, variant, className }: TabListProps<Id>) {
  const style = VARIANTS[variant];
  const elements = useRef(new Map<Id, HTMLButtonElement | null>());

  const enabled = tabs.items.filter((item) => !item.disabled);
  const selectedIndex = enabled.findIndex((item) => item.id === tabs.value);
  // A value matching no enabled tab (a strip whose selection is still being
  // resolved) still needs exactly one tab stop: the first enabled tab.
  const stopId = selectedIndex >= 0 ? tabs.value : enabled[0]?.id;

  const focusAt = (index: number) => {
    const target = enabled[(index + enabled.length) % enabled.length];
    if (!target) return;
    tabs.onChange(target.id);
    const element = elements.current.get(target.id);
    element?.focus();
    // Keep a focused tab visible in an overflowing strip. No `behavior`, so
    // nothing animates and there is nothing for reduced motion to suppress.
    element?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const from = selectedIndex >= 0 ? selectedIndex : 0;
    switch (event.key) {
      case 'ArrowRight':
        focusAt(from + 1);
        break;
      case 'ArrowLeft':
        focusAt(from - 1);
        break;
      case 'ArrowDown':
        if (!style.verticalKeys) return;
        focusAt(from + 1);
        break;
      case 'ArrowUp':
        if (!style.verticalKeys) return;
        focusAt(from - 1);
        break;
      case 'Home':
        focusAt(0);
        break;
      case 'End':
        focusAt(enabled.length - 1);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      // No `aria-orientation`: `underline` and `pill` are horizontal (the
      // default), and `rail` is horizontal or vertical depending on viewport
      // width, which no static value states honestly. Both arrow axes work
      // there instead.
      className={`${style.list}${className ? ` ${className}` : ''}`}
    >
      {tabs.items.map((item) => {
        const selected = item.id === tabs.value;
        const Icon = item.icon;
        return (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={tabs.tabId(item.id)}
            ref={(element) => {
              elements.current.set(item.id, element);
            }}
            aria-selected={selected}
            aria-controls={selected ? tabs.panelId(item.id) : undefined}
            aria-disabled={item.disabled || undefined}
            tabIndex={item.id === stopId ? 0 : -1}
            onClick={() => {
              if (!item.disabled) tabs.onChange(item.id);
            }}
            onKeyDown={handleKeyDown}
            className={`${style.tab} ${FOCUS_RING} ${
              selected ? style.selected : style.idle
            }${item.disabled ? ' opacity-40 cursor-not-allowed' : ''}`}
          >
            {Icon && <Icon className="w-4 h-4" aria-hidden="true" />}
            {item.label}
            {item.badge}
          </button>
        );
      })}
    </div>
  );
}

export interface TabPanelProps<Id extends string> {
  tabs: TabsModel<Id>;
  children: ReactNode;
  className?: string;
}

/**
 * The active panel's container. One element, carrying the SELECTED tab's panel
 * id and labelled by that tab — the sites here render one panel at a time, and
 * keeping that (rather than mounting every panel hidden) is what makes the
 * Settings page's thirteen query-driven sections cost one section's queries.
 *
 * No `tabIndex={0}`: every panel in this app contains focusable controls, so
 * the APG's "focusable panel" fallback would only add a second tab stop. A
 * future panel made entirely of static text would need it.
 *
 * The label is dropped when no rendered tab carries the current value. A
 * permission-filtered strip can hold exactly that state — the Settings search
 * jumps straight to a sub-tab, and a non-admin has no LDAP tab to land on —
 * and an unlabelled panel is valid where a reference to an absent tab is not.
 */
export function TabPanel<Id extends string>({ tabs, children, className }: TabPanelProps<Id>) {
  const labelled = tabs.items.some((item) => item.id === tabs.value);
  return (
    <div
      id={tabs.panelId(tabs.value)}
      role="tabpanel"
      aria-labelledby={labelled ? tabs.tabId(tabs.value) : undefined}
      className={className}
    >
      {children}
    </div>
  );
}
