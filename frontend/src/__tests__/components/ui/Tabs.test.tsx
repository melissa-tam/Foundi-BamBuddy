/**
 * Tabs — the app's one tablist primitive.
 *
 * What these pin is everything the hand-rolled strips did NOT do: the strip is
 * a single tab stop rather than one per tab, arrow keys move between tabs,
 * each tab is bound to the panel it swaps, and a disabled tab is skipped by
 * the keyboard instead of swallowing focus.
 *
 * Queries are by role and accessible name only — never by class or copy — so
 * these survive a restyle and a copy edit.
 */
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '../../utils';
import userEvent from '@testing-library/user-event';
import { TabList, TabPanel, type TabsVariant } from '../../../components/ui/Tabs';
import { useTabs, type TabDefinition } from '../../../hooks/useTabs';

type Id = 'queue' | 'history' | 'timeline';

const ITEMS: TabDefinition<Id>[] = [
  { id: 'queue', label: 'Queue' },
  { id: 'history', label: 'History' },
  { id: 'timeline', label: 'Timeline' },
];

interface HarnessProps {
  items?: TabDefinition<Id>[];
  initial?: Id;
  variant?: TabsVariant;
  onChange?: (id: Id) => void;
}

function Harness({ items = ITEMS, initial = 'queue', variant = 'underline', onChange }: HarnessProps) {
  const [value, setValue] = useState<Id>(initial);
  const tabs = useTabs<Id>({
    value,
    onChange: (id) => {
      onChange?.(id);
      setValue(id);
    },
    items,
  });
  return (
    <div>
      <button type="button">before</button>
      <TabList tabs={tabs} ariaLabel="Queue views" variant={variant} />
      <TabPanel tabs={tabs}>
        <button type="button">{`inside ${value}`}</button>
      </TabPanel>
      <button type="button">after</button>
    </div>
  );
}

const tab = (name: string) => screen.getByRole('tab', { name });

describe('Tabs — roles and wiring', () => {
  it('names the tablist and exposes one tab per item', () => {
    render(<Harness />);

    const list = screen.getByRole('tablist', { name: 'Queue views' });
    expect(within(list).getAllByRole('tab')).toHaveLength(3);
  });

  it('binds the selected tab to the panel in both directions', () => {
    render(<Harness initial="history" />);

    const selected = tab('History');
    const panel = screen.getByRole('tabpanel');

    expect(selected).toHaveAttribute('aria-selected', 'true');
    expect(selected.getAttribute('aria-controls')).toBe(panel.id);
    expect(panel.getAttribute('aria-labelledby')).toBe(selected.id);
  });

  it('leaves aria-controls off tabs whose panel is not rendered', () => {
    render(<Harness />);

    // A dangling IDREF is worse than an absent one: every unselected panel is
    // out of the DOM, because only the active panel renders.
    expect(tab('History')).not.toHaveAttribute('aria-controls');
    expect(tab('History')).toHaveAttribute('aria-selected', 'false');
  });

  it('leaves the panel unlabelled when no rendered tab holds the value', () => {
    // A permission-filtered strip: the Settings search can jump a non-admin
    // straight to a sub-tab whose tab is not in their list.
    render(<Harness items={ITEMS.slice(0, 2)} initial="timeline" />);

    const panel = screen.getByRole('tabpanel');
    expect(panel).not.toHaveAttribute('aria-labelledby');
    // The strip still has exactly one tab stop, on its first enabled tab.
    expect(tab('Queue')).toHaveAttribute('tabindex', '0');
    expect(tab('History')).toHaveAttribute('tabindex', '-1');
  });

  it('renders only the active panel', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'inside queue' })).toBeInTheDocument();

    await user.click(tab('Timeline'));

    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'inside timeline' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'inside queue' })).not.toBeInTheDocument();
  });
});

describe('Tabs — one tab stop', () => {
  it('gives the strip a single tab stop on the selected tab', () => {
    render(<Harness initial="history" />);

    expect(tab('Queue')).toHaveAttribute('tabindex', '-1');
    expect(tab('History')).toHaveAttribute('tabindex', '0');
    expect(tab('Timeline')).toHaveAttribute('tabindex', '-1');
  });

  it('steps Tab straight through the strip into the panel', async () => {
    const user = userEvent.setup();
    render(<Harness />);

    screen.getByRole('button', { name: 'before' }).focus();
    await user.tab();
    expect(tab('Queue')).toHaveFocus();

    // The other two tabs are NOT stops: the next Tab reaches the panel.
    await user.tab();
    expect(screen.getByRole('button', { name: 'inside queue' })).toHaveFocus();
  });
});

describe('Tabs — keyboard', () => {
  it('moves selection and focus with the arrow keys, wrapping at both ends', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    tab('Queue').focus();

    await user.keyboard('{ArrowRight}');
    expect(tab('History')).toHaveFocus();
    expect(tab('History')).toHaveAttribute('aria-selected', 'true');
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenLastCalledWith('history');

    await user.keyboard('{ArrowRight}{ArrowRight}');
    expect(tab('Queue')).toHaveFocus();
    expect(onChange).toHaveBeenCalledTimes(3);
    expect(onChange).toHaveBeenLastCalledWith('queue');

    await user.keyboard('{ArrowLeft}');
    expect(tab('Timeline')).toHaveFocus();
    expect(onChange).toHaveBeenCalledTimes(4);
    expect(onChange).toHaveBeenLastCalledWith('timeline');
  });

  it('jumps to the ends with Home and End', async () => {
    const user = userEvent.setup();
    render(<Harness initial="history" />);

    tab('History').focus();

    await user.keyboard('{End}');
    expect(tab('Timeline')).toHaveFocus();
    expect(tab('Timeline')).toHaveAttribute('aria-selected', 'true');

    await user.keyboard('{Home}');
    expect(tab('Queue')).toHaveFocus();
    expect(tab('Queue')).toHaveAttribute('aria-selected', 'true');
  });

  it('skips a disabled tab and refuses to select it', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const items: TabDefinition<Id>[] = [
      { id: 'queue', label: 'Queue' },
      { id: 'history', label: 'History', disabled: true },
      { id: 'timeline', label: 'Timeline' },
    ];
    render(<Harness items={items} onChange={onChange} />);

    // Focusable and named, so its tooltip can say WHY it is unavailable.
    expect(tab('History')).toHaveAttribute('aria-disabled', 'true');

    tab('Queue').focus();
    await user.keyboard('{ArrowRight}');
    expect(tab('Timeline')).toHaveFocus();

    await user.click(tab('History'));
    expect(onChange).not.toHaveBeenCalledWith('history');
    expect(tab('History')).toHaveAttribute('aria-selected', 'false');
  });

  it('ignores the vertical arrows on a horizontal strip', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);

    tab('Queue').focus();
    await user.keyboard('{ArrowDown}');

    expect(tab('Queue')).toHaveFocus();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('answers both axes on the rail, which is vertical above lg', async () => {
    const user = userEvent.setup();
    render(<Harness variant="rail" />);

    tab('Queue').focus();
    await user.keyboard('{ArrowDown}');
    expect(tab('History')).toHaveFocus();

    await user.keyboard('{ArrowUp}');
    expect(tab('Queue')).toHaveFocus();
  });
});

describe('Tabs — variants and adornments', () => {
  it.each<TabsVariant>(['underline', 'pill', 'rail'])('renders the %s variant as a tablist', (variant) => {
    render(<Harness variant={variant} />);

    expect(screen.getByRole('tablist', { name: 'Queue views' })).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(3);
  });

  it('keeps a trailing badge inside the tab and an icon out of its name', () => {
    const items: TabDefinition<Id>[] = [
      { id: 'queue', label: 'Queue', badge: <span>4</span> },
      { id: 'history', label: 'History' },
      { id: 'timeline', label: 'Timeline' },
    ];
    render(<Harness items={items} />);

    // The count is part of the tab's accessible name, as it was in the
    // hand-rolled strips: "Queue, 4 items" is the information, not decoration.
    expect(screen.getByRole('tab', { name: 'Queue 4' })).toBeInTheDocument();
  });
});
