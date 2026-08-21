/**
 * Tests for the FilamentSlotCircle component.
 */

import { describe, it, expect } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import { render } from '../utils';
import { FilamentSlotCircle } from '../../components/FilamentSlotCircle';

/**
 * JSDOM normalizes some CSS color values (e.g. #000 → rgb(0, 0, 0)),
 * so we compare against both hex and rgb forms.
 */
function expectColor(actual: string, hex: string, rgb: string) {
  expect([hex, rgb]).toContain(actual);
}

describe('FilamentSlotCircle', () => {
  it('renders the slot number', () => {
    render(<FilamentSlotCircle trayColor="FF0000" trayType="PLA" isEmpty={false} slotNumber={1} />);
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('renders slot number for empty slot', () => {
    render(<FilamentSlotCircle isEmpty={true} slotNumber={3} />);
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('uses dashed border for empty slots', () => {
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('dashed');
  });

  it('uses solid border for filled slots', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.borderStyle).toBe('solid');
  });

  it('sets background color from trayColor', () => {
    const { container } = render(
      <FilamentSlotCircle trayColor="00FF00" trayType="PLA" isEmpty={false} slotNumber={2} />
    );
    const circle = container.firstChild as HTMLElement;
    expectColor(circle.style.backgroundColor, '#00FF00', 'rgb(0, 255, 0)');
  });

  it('uses dark background when trayType is set but no color', () => {
    const { container } = render(
      <FilamentSlotCircle trayType="PLA" isEmpty={false} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expectColor(circle.style.backgroundColor, '#333', 'rgb(51, 51, 51)');
  });

  it('uses transparent background when empty and no type', () => {
    const { container } = render(
      <FilamentSlotCircle isEmpty={true} slotNumber={1} />
    );
    const circle = container.firstChild as HTMLElement;
    expect(circle.style.backgroundColor).toBe('transparent');
  });

  it('uses black text on light filament colors', () => {
    // White filament (FFFFFF) is light
    render(<FilamentSlotCircle trayColor="FFFFFF" isEmpty={false} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#000', 'rgb(0, 0, 0)');
  });

  it('uses white text on dark filament colors', () => {
    // Black filament (000000) is dark
    render(<FilamentSlotCircle trayColor="000000" isEmpty={false} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#fff', 'rgb(255, 255, 255)');
  });

  it('uses white text when no tray color', () => {
    render(<FilamentSlotCircle isEmpty={true} slotNumber={1} />);
    const text = screen.getByText('1');
    expectColor(text.style.color, '#fff', 'rgb(255, 255, 255)');
  });

  describe('seated-but-unread slot (emptyKind "present")', () => {
    it('renders a "?" glyph instead of the slot number', () => {
      render(<FilamentSlotCircle isEmpty={true} emptyKind="present" slotNumber={3} />);
      expect(screen.getByText('?')).toBeInTheDocument();
      expect(screen.queryByText('3')).not.toBeInTheDocument();
    });

    it('labels the glyph so the state is never colour-only', () => {
      render(<FilamentSlotCircle isEmpty={true} emptyKind="present" slotNumber={3} />);
      const glyph = screen.getByRole('img');
      const label = glyph.getAttribute('aria-label');
      expect(label).toBeTruthy();
      // The key must have RESOLVED — a raw key here means a missing locale entry.
      expect(label).not.toBe('ams.slotPresentUnread');
      // title carries the same text for hover/keyboard discovery.
      expect(glyph.getAttribute('title')).toBe(label);
    });

    it('uses a solid border so it cannot be mistaken for an empty slot', () => {
      const { container } = render(
        <FilamentSlotCircle isEmpty={true} emptyKind="present" slotNumber={1} />
      );
      const circle = container.firstChild as HTMLElement;
      expect(circle.style.borderStyle).toBe('solid');
      // The warning-tone ring comes from theme-paired classes, so no inline
      // borderColor may be set (it would override them).
      expect(circle.style.borderColor).toBe('');
      expect(circle.className).toContain('border-amber-600');
      expect(circle.className).toContain('dark:border-amber-400');
    });

    it('keeps the dashed empty look for physical and unknown slots', () => {
      for (const kind of ['physical', 'unknown'] as const) {
        const { container, unmount } = render(
          <FilamentSlotCircle isEmpty={true} emptyKind={kind} slotNumber={2} />
        );
        const circle = container.firstChild as HTMLElement;
        expect(circle.style.borderStyle).toBe('dashed');
        expect(circle.className).not.toContain('border-amber-600');
        expect(screen.getByText('2')).toBeInTheDocument();
        expect(screen.queryByRole('img')).not.toBeInTheDocument();
        unmount();
      }
    });

    it('gives an unknown-presence slot a dimmer ring and its own stated reason', () => {
      // Quiet, but never silent: "we do not know" must be readable, and it must
      // not look identical to the positive claim "nothing is in there".
      const { container: unknownC, unmount } = render(
        <FilamentSlotCircle isEmpty={true} emptyKind="unknown" slotNumber={2} />
      );
      const unknownCircle = unknownC.firstChild as HTMLElement;
      const title = unknownCircle.getAttribute('title');
      expect(title).toBeTruthy();
      // A raw key here would mean a missing locale entry.
      expect(title).not.toBe('ams.slotStateUnknown');
      // Carried for screen readers too, not by the border tone alone.
      expect(screen.getByText(title as string)).toBeInTheDocument();
      const dimmed = unknownCircle.style.borderColor;
      unmount();

      const { container: physicalC } = render(
        <FilamentSlotCircle isEmpty={true} emptyKind="physical" slotNumber={2} />
      );
      const physicalCircle = physicalC.firstChild as HTMLElement;
      expect(physicalCircle.getAttribute('title')).toBeNull();
      expect(physicalCircle.style.borderColor).not.toBe(dimmed);
    });

    it('ignores emptyKind "present" when the slot is loaded', () => {
      const { container } = render(
        <FilamentSlotCircle trayColor="FF0000" trayType="PETG" isEmpty={false} emptyKind="present" slotNumber={4} />
      );
      const circle = container.firstChild as HTMLElement;
      expect(screen.getByText('4')).toBeInTheDocument();
      expect(screen.queryByText('?')).not.toBeInTheDocument();
      expectColor(circle.style.borderColor, 'rgba(255,255,255,0.1)', 'rgba(255, 255, 255, 0.1)');
    });
  });

  describe('out-of-rotation badge', () => {
    it('renders the warning badge when outOfRotation is true', () => {
      render(
        <FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} outOfRotation />
      );
      // Badge is an accessible image with an aria-label (not colour-only).
      const badge = screen.getByRole('img');
      expect(badge).toBeInTheDocument();
    });

    it('gives the badge a non-empty aria-label and a matching title', () => {
      render(
        <FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} outOfRotation />
      );
      const badge = screen.getByRole('img');
      const label = badge.getAttribute('aria-label');
      expect(label).toBeTruthy();
      // title carries the same tooltip text for hover/keyboard discovery.
      expect(badge.getAttribute('title')).toBe(label);
    });

    it('does not render the badge when outOfRotation is false', () => {
      render(
        <FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} outOfRotation={false} />
      );
      expect(screen.queryByRole('img')).not.toBeInTheDocument();
    });

    it('does not render the badge when outOfRotation is omitted', () => {
      render(<FilamentSlotCircle trayColor="FF0000" isEmpty={false} slotNumber={1} />);
      expect(screen.queryByRole('img')).not.toBeInTheDocument();
    });
  });

  describe('no-backup-slot badge', () => {
    /** Render the badge alone and hand back its resolved label. */
    function renderLoneBadge() {
      render(
        <FilamentSlotCircle trayColor="161616" isEmpty={false} slotNumber={2} noBackupSlot />
      );
      const badge = screen.getByRole('img');
      const label = badge.getAttribute('aria-label');
      return { badge, label };
    }

    it('renders a labelled badge when noBackupSlot is true and the slot is loaded', () => {
      const { badge, label } = renderLoneBadge();
      expect(label).toBeTruthy();
      // A raw key here would mean a missing locale entry.
      expect(label).not.toBe('printers.slot.noBackupSlot');
      // title carries the same text for hover/keyboard discovery.
      expect(badge.getAttribute('title')).toBe(label);
    });

    it('states the reason for screen readers, never by colour alone (WCAG 1.4.1)', () => {
      const { label } = renderLoneBadge();
      // The sentence IS the badge's accessible name — the meaning never rides
      // on the amber tone. Queried by role+name exactly as AT resolves it.
      expect(screen.getByRole('img', { name: label as string })).toBeInTheDocument();
    });

    it('does not render the badge when noBackupSlot is false', () => {
      render(
        <FilamentSlotCircle trayColor="161616" isEmpty={false} slotNumber={2} noBackupSlot={false} />
      );
      expect(screen.queryByRole('img')).not.toBeInTheDocument();
    });

    it('does not render the badge on an empty slot even when noBackupSlot is true', () => {
      // An empty slot has no filament to back up — the badge would claim
      // something about nothing.
      render(<FilamentSlotCircle isEmpty={true} slotNumber={2} noBackupSlot />);
      expect(screen.queryByRole('img')).not.toBeInTheDocument();
    });

    it('coexists with the other three badges', () => {
      const { label } = renderLoneBadge();
      cleanup();

      render(
        <FilamentSlotCircle
          trayColor="161616"
          isEmpty={false}
          slotNumber={2}
          outOfRotation
          ranOut
          spentCore
          noBackupSlot
        />
      );
      const badges = screen.getAllByRole('img');
      expect(badges).toHaveLength(4);
      const labels = badges.map((b) => b.getAttribute('aria-label'));
      // Four distinct, resolved labels — one corner each.
      expect(new Set(labels).size).toBe(4);
      expect(labels).toContain(label);
      for (const each of labels) {
        expect(each).toBeTruthy();
        expect(each).not.toMatch(/^(printers|ams)\./);
      }
    });
  });
});
