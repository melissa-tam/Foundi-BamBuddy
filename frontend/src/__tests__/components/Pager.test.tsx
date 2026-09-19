/**
 * Pager — the shared pagination bar.
 *
 * The defect these pin (browser-verified, WCAG 2.4.3 Focus Order): the four
 * nav buttons used the native `disabled` attribute, so paging onto the last
 * page disabled the very button the keyboard user had just activated. A
 * natively disabled control leaves the focus order immediately, and focus fell
 * to `document.body` — the user lost their place in the page. Same on the
 * other end with Previous/First landing on page 1.
 *
 * The contract is therefore: an unavailable nav button stays focusable, says
 * `aria-disabled`, keeps its accessible name, and does nothing when activated.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Pager, PAGE_SIZE_ALL } from '../../components/ui/Pager';

// Echo the key so the assertions read as the key, without the i18n runtime.
const t = (k: string) => k;

function renderPager(overrides: Partial<React.ComponentProps<typeof Pager>> = {}) {
  const onPageChange = vi.fn();
  const onPageSizeChange = vi.fn();
  render(
    <Pager
      pageIndex={1}
      pageSize={50}
      totalRows={120}
      totalPages={3}
      onPageChange={onPageChange}
      onPageSizeChange={onPageSizeChange}
      unitLabel="prints"
      t={t}
      {...overrides}
    />,
  );
  return { onPageChange, onPageSizeChange };
}

const nav = {
  first: () => screen.getByRole('button', { name: 'common.pagination.first' }),
  previous: () => screen.getByRole('button', { name: 'common.pagination.previous' }),
  next: () => screen.getByRole('button', { name: 'common.pagination.next' }),
  last: () => screen.getByRole('button', { name: 'common.pagination.last' }),
};

describe('Pager — keyboard focus is never dropped', () => {
  it('keeps focus on Next after activating it onto the LAST page', async () => {
    const user = userEvent.setup();
    // Already on the last page: Next is unavailable.
    const { onPageChange } = renderPager({ pageIndex: 2 });

    const next = nav.next();
    next.focus();
    expect(next).toHaveFocus();

    await user.keyboard('{Enter}');

    expect(onPageChange).not.toHaveBeenCalled();
    // The control did not leave the focus order — the whole point.
    expect(next).toHaveFocus();
    expect(document.activeElement).not.toBe(document.body);
  });

  it('keeps focus on Last after activating it onto the LAST page', async () => {
    const user = userEvent.setup();
    const { onPageChange } = renderPager({ pageIndex: 2 });

    const last = nav.last();
    last.focus();
    await user.keyboard('{Enter}');

    expect(onPageChange).not.toHaveBeenCalled();
    expect(last).toHaveFocus();
  });

  it('keeps focus on Previous after activating it on page 1', async () => {
    const user = userEvent.setup();
    const { onPageChange } = renderPager({ pageIndex: 0 });

    const previous = nav.previous();
    previous.focus();
    await user.keyboard('{Enter}');

    expect(onPageChange).not.toHaveBeenCalled();
    expect(previous).toHaveFocus();
    expect(document.activeElement).not.toBe(document.body);
  });

  it('keeps focus on First after activating it on page 1', async () => {
    const user = userEvent.setup();
    const { onPageChange } = renderPager({ pageIndex: 0 });

    const first = nav.first();
    first.focus();
    await user.keyboard('{Enter}');

    expect(onPageChange).not.toHaveBeenCalled();
    expect(first).toHaveFocus();
  });
});

describe('Pager — unavailable is announced, not removed', () => {
  it('marks Next/Last aria-disabled on the last page, and keeps their names', () => {
    renderPager({ pageIndex: 2 });

    expect(nav.next()).toHaveAttribute('aria-disabled', 'true');
    expect(nav.last()).toHaveAttribute('aria-disabled', 'true');
    // Backwards navigation is still offered.
    expect(nav.first()).toHaveAttribute('aria-disabled', 'false');
    expect(nav.previous()).toHaveAttribute('aria-disabled', 'false');
    // Never the native attribute — that is what dropped focus.
    expect(nav.next()).toBeEnabled();
    expect(nav.last()).toBeEnabled();
  });

  it('marks First/Previous aria-disabled on page 1', () => {
    renderPager({ pageIndex: 0 });

    expect(nav.first()).toHaveAttribute('aria-disabled', 'true');
    expect(nav.previous()).toHaveAttribute('aria-disabled', 'true');
    expect(nav.next()).toHaveAttribute('aria-disabled', 'false');
    expect(nav.last()).toHaveAttribute('aria-disabled', 'false');
    expect(nav.first()).toBeEnabled();
  });

  it('ignores a CLICK on an unavailable control too', async () => {
    const user = userEvent.setup();
    const { onPageChange } = renderPager({ pageIndex: 0 });

    await user.click(nav.previous());
    await user.click(nav.first());

    expect(onPageChange).not.toHaveBeenCalled();
  });

  it('every nav button is reachable by keyboard', async () => {
    const user = userEvent.setup();
    renderPager({ pageIndex: 0 });

    const reached: (string | null)[] = [];
    for (let i = 0; i < 5; i += 1) {
      await user.tab();
      reached.push(document.activeElement?.getAttribute('aria-label') ?? null);
    }

    expect(reached).toContain('common.pagination.first');
    expect(reached).toContain('common.pagination.previous');
    expect(reached).toContain('common.pagination.next');
    expect(reached).toContain('common.pagination.last');
  });
});

describe('Pager — paging still works', () => {
  it('advances and rewinds from a middle page', async () => {
    const user = userEvent.setup();
    const { onPageChange } = renderPager({ pageIndex: 1 });

    await user.click(nav.next());
    expect(onPageChange).toHaveBeenLastCalledWith(2);

    await user.click(nav.previous());
    expect(onPageChange).toHaveBeenLastCalledWith(0);

    await user.click(nav.first());
    expect(onPageChange).toHaveBeenLastCalledWith(0);

    await user.click(nav.last());
    expect(onPageChange).toHaveBeenLastCalledWith(2);
  });

  it('reports the visible range and the page number', () => {
    renderPager({ pageIndex: 1 });
    expect(screen.getByText(/common\.pagination\.showing/)).toBeInTheDocument();
    expect(screen.getByText(/common\.pagination\.page/)).toBeInTheDocument();
  });

  it('renders nothing when there is only one page', () => {
    const { container } = render(
      <Pager
        pageIndex={0}
        pageSize={50}
        totalRows={3}
        totalPages={1}
        onPageChange={vi.fn()}
        onPageSizeChange={vi.fn()}
        unitLabel="prints"
        t={t}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('under "All" states the row count and offers no nav buttons', () => {
    renderPager({ pageSize: PAGE_SIZE_ALL, pageIndex: 0, totalPages: 1 });

    expect(screen.getByText('120 prints')).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'common.pagination.next' }),
    ).not.toBeInTheDocument();
  });

  it('changes the page size', async () => {
    const user = userEvent.setup();
    const { onPageSizeChange } = renderPager();

    await user.selectOptions(screen.getByRole('combobox'), '100');

    expect(onPageSizeChange).toHaveBeenCalledWith(100);
  });
});
