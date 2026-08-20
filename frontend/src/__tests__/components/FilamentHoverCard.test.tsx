/**
 * Tests for the FilamentHoverCard component.
 * Focuses on fill level display and Spoolman source indicator.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '../utils';
import { FilamentHoverCard, EmptySlotHoverCard } from '../../components/FilamentHoverCard';

const baseFilamentData = {
  vendor: 'Bambu Lab' as const,
  profile: 'PLA Basic',
  colorName: 'Red',
  colorHex: 'FF0000',
  kFactor: '0.030',
  fillLevel: 75,
  trayUuid: 'A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4',
};

function renderWithHover(ui: React.ReactElement) {
  const result = render(ui);
  // Trigger hover to show the card
  const trigger = result.container.firstElementChild as HTMLElement;
  fireEvent.mouseEnter(trigger);
  return result;
}

describe('FilamentHoverCard', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  describe('fill level display', () => {
    it('shows fill percentage when fillLevel is set', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 75 }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('75%')).toBeInTheDocument();
      });
    });

    it('shows dash when fillLevel is null', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: null }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('—')).toBeInTheDocument();
      });
    });

    it('shows 0% when fillLevel is zero', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 0 }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('0%')).toBeInTheDocument();
      });
    });
  });

  describe('fill source badge transparency (#11)', () => {
    it('never shows a Spoolman-source badge — UI stays mode-agnostic', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'spoolman' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText('80%')).toBeInTheDocument();
        expect(screen.queryByText('(Spoolman)')).not.toBeInTheDocument();
      });
    });

    it('never shows an inventory-source badge — UI stays mode-agnostic', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'inventory' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText('80%')).toBeInTheDocument();
        expect(screen.queryByText('(Inv)')).not.toBeInTheDocument();
      });
    });

    it('does not render an empty source-label span when fillLevel is null', async () => {
      renderWithHover(
        <FilamentHoverCard data={{ ...baseFilamentData, fillLevel: null, fillSource: 'spoolman' }}>
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText('—')).toBeInTheDocument();
        expect(screen.queryByText('(Spoolman)')).not.toBeInTheDocument();
        expect(screen.queryByText('(Inv)')).not.toBeInTheDocument();
      });
    });
  });

  describe('hover behavior', () => {
    it('does not show card when disabled', () => {
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData} disabled>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      // Card should not be visible
      expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument();
    });

    it('shows filament details on hover', async () => {
      renderWithHover(
        <FilamentHoverCard data={baseFilamentData}>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      await waitFor(() => {
        expect(screen.getByText('Red')).toBeInTheDocument();
        expect(screen.getByText('PLA Basic')).toBeInTheDocument();
        expect(screen.getByText('0.030')).toBeInTheDocument();
      });
    });
  });

  // The inventory section was previously hidden for `vendor === 'Bambu Lab'`
  // because BL spools were assumed to be managed entirely via RFID. #1133
  // removed that gate so users who don't want to scan via SpoolBuddy NFC
  // can still pick a BL spool from inventory the same way they pick a
  // third-party one.
  describe('inventory section vendor visibility (#1133)', () => {
    it('shows the assign-spool button on a Bambu Lab slot when the spool is unassigned', async () => {
      const onAssign = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={{ ...baseFilamentData, vendor: 'Bambu Lab' }}
          inventory={{ assignedSpool: null, onAssignSpool: onAssign }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assign/i)).toBeInTheDocument();
      });
    });

    it('shows the unassign button on a Bambu Lab slot when an inventory spool is already assigned', async () => {
      // Regression guard: the original gate hid BOTH the assign and unassign
      // buttons for BL slots. A user who'd already assigned an inventory
      // spool to a BL slot couldn't undo it without dropping into the
      // inventory page directly.
      const onUnassign = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={{ ...baseFilamentData, vendor: 'Bambu Lab' }}
          inventory={{
            assignedSpool: {
              id: 1,
              material: 'PLA',
              brand: 'Devil Design',
              color_name: 'Black',
            },
            onUnassignSpool: onUnassign,
          }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/unassign/i)).toBeInTheDocument();
      });
    });

    it('still shows the assign-spool button for a non-Bambu vendor (no behaviour change)', async () => {
      const onAssign = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={{ ...baseFilamentData, vendor: 'Polymaker' as unknown as 'Bambu Lab' }}
          inventory={{ assignedSpool: null, onAssignSpool: onAssign }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assign/i)).toBeInTheDocument();
      });
    });

    it('shows the assign-spool button as disabled when isAssigned=true', async () => {
      const onAssign = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={{ ...baseFilamentData, vendor: 'Bambu Lab' }}
          inventory={{ assignedSpool: null, onAssignSpool: onAssign, isAssigned: true }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assign/i)).toBeInTheDocument();
        expect(screen.getByText(/assign/i).closest('button')).toBeDisabled();
      });
    });

    it('does not call onAssignSpool when the button is disabled via isAssigned', async () => {
      const onAssign = vi.fn();
      renderWithHover(
        <FilamentHoverCard
          data={{ ...baseFilamentData, vendor: 'Bambu Lab' }}
          inventory={{ assignedSpool: null, onAssignSpool: onAssign, isAssigned: true }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => expect(screen.getByText(/assign/i)).toBeInTheDocument());
      const btn = screen.getByText(/assign/i).closest('button')!;
      btn.click();
      expect(onAssign).not.toHaveBeenCalled();
    });
  });

  // For RFID-synced BL spools, both spoolman.linkedSpoolId and
  // inventory.assignedSpool.id point to the same Spoolman spool. Rendering
  // both branches gave two identical "Open in Inventory" buttons. The
  // inventory-side button is suppressed when it would duplicate the
  // spoolman-side link.
  describe('"Open in Inventory" deduplication', () => {
    const inventorySpool = {
      id: 42,
      material: 'PLA',
      brand: 'eSun',
      color_name: 'Black',
    };

    it('shows only one Open in Inventory button when spoolman linkedSpoolId matches assignedSpool id', async () => {
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          spoolman={{ enabled: true, linkedSpoolId: 42 }}
          inventory={{ assignedSpool: inventorySpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assigned/i)).toBeInTheDocument();
      });
      expect(screen.queryAllByTitle('Open in Inventory')).toHaveLength(1);
    });

    it('shows two Open in Inventory buttons when spoolman and inventory point to different spools', async () => {
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          spoolman={{ enabled: true, linkedSpoolId: 99 }}
          inventory={{ assignedSpool: inventorySpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assigned/i)).toBeInTheDocument();
      });
      expect(screen.queryAllByTitle('Open in Inventory')).toHaveLength(2);
    });

    it('shows one Open in Inventory button when spoolman is absent but inventory spool is assigned', async () => {
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          inventory={{ assignedSpool: inventorySpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText(/assigned/i)).toBeInTheDocument();
      });
      expect(screen.queryAllByTitle('Open in Inventory')).toHaveLength(1);
    });

    it('shows the spool ID in the assigned-spool block', async () => {
      renderWithHover(
        <FilamentHoverCard
          data={baseFilamentData}
          inventory={{ assignedSpool: inventorySpool }}
        >
          <div>trigger</div>
        </FilamentHoverCard>
      );
      vi.advanceTimersByTime(100);
      await waitFor(() => {
        expect(screen.getByText('#42')).toBeInTheDocument();
      });
    });
  });
});

// EmptySlotHoverCard is the hover wrapper rendered for a physically empty
// AMS slot. #1133 removed its inventory affordance: a slot with nothing
// loaded has no spool to attach an inventory record to, and offering the
// action there only led to users assigning the wrong spool to a slot the
// printer hadn't actually loaded yet. The configure-slot affordance is
// kept, since "preset for the next spool to land here" is still a sensible
// thing to do on an empty slot.
describe('EmptySlotHoverCard (#1133)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it('does not render an assign-spool button when onAssignSpool is not provided', async () => {
    const result = render(
      <EmptySlotHoverCard configureSlot={{ enabled: true, onConfigure: vi.fn() }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => {
      // The card itself is showing — guard the negative assertion against
      // a card that simply never opened.
      expect(screen.getByText(/empty/i)).toBeInTheDocument();
    });
    expect(screen.queryByText(/assign spool/i)).not.toBeInTheDocument();
  });

  it('still shows the configure button on an empty slot', async () => {
    const onConfigure = vi.fn();
    const result = render(
      <EmptySlotHoverCard configureSlot={{ enabled: true, onConfigure }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => {
      expect(screen.getByText(/configure/i)).toBeInTheDocument();
    });
  });

  it('shows Assign Spool button when onAssignSpool is provided', async () => {
    const onAssign = vi.fn();
    const result = render(
      <EmptySlotHoverCard onAssignSpool={onAssign}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => {
      expect(screen.getByText(/assign spool/i)).toBeInTheDocument();
    });
  });

  it('calls onAssignSpool when Assign Spool button is clicked', async () => {
    const onAssign = vi.fn();
    const result = render(
      <EmptySlotHoverCard onAssignSpool={onAssign}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText(/assign spool/i)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/assign spool/i));
    expect(onAssign).toHaveBeenCalledTimes(1);
  });
});

// W5a — a binding that outlives the filament. Before this the empty-slot card
// carried NO assignment information at all, so a stale or latched claim was
// both invisible and unclearable from the printer card (the 2026-08 stale-empty
// class of incident). The three states must be distinguishable by TEXT, not by
// colour alone.
describe('EmptySlotHoverCard binding block (W5a)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  const baseBinding = {
    spoolId: 140,
    label: 'Overture PETG - Black',
    usedGrams: 820,
    spent: false,
    // The slot is verifiably empty unless a test says otherwise — that is what
    // makes "not inserted" a statement the card is entitled to make.
    preConfigured: false,
    presence: 'empty' as const,
  };

  /** `header` is what the card's first line reads once open — it changes with
   *  the slot kind, so a seated-unread card is awaited on its own wording. */
  async function openCard(ui: React.ReactElement, header: RegExp = /empty/i) {
    const result = render(ui);
    fireEvent.mouseEnter(result.container.firstElementChild as HTMLElement);
    vi.advanceTimersByTime(100);
    await waitFor(() => expect(screen.getByText(header)).toBeInTheDocument());
    return result;
  }

  it('shows no binding block and no clear verb when nothing is bound', async () => {
    await openCard(
      <EmptySlotHoverCard onClearSlot={vi.fn()}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.queryByText(/still assigned/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument();
  });

  it('names the bound spool, its record id and the grams already charged to it', async () => {
    await openCard(
      <EmptySlotHoverCard binding={baseBinding}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/still assigned/i)).toBeInTheDocument();
    expect(screen.getByText('Overture PETG - Black')).toBeInTheDocument();
    expect(screen.getByText('#140')).toBeInTheDocument();
    expect(screen.getByText('820 g used')).toBeInTheDocument();
  });

  it('reads "not inserted" for a lingering binding on a verifiably empty slot', async () => {
    await openCard(
      <EmptySlotHoverCard binding={baseBinding}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/not inserted/i)).toBeInTheDocument();
  });

  it('never says "not inserted" beneath a header saying a spool IS present', async () => {
    // The contradiction the operator photographed: the card header read
    // "Spool present — unrecognized" while the binding line under it read
    // "Still assigned … not inserted".
    await openCard(
      <EmptySlotHoverCard kind="present" binding={{ ...baseBinding, presence: 'seated' }}>
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /spool present/i,
    );
    expect(screen.getByText(/awaiting identification/i)).toBeInTheDocument();
    expect(screen.queryByText(/not inserted/i)).not.toBeInTheDocument();
  });

  it('states unconfirmed, not empty, when the slot presence is unknown', async () => {
    await openCard(
      <EmptySlotHoverCard kind="unknown" binding={{ ...baseBinding, presence: 'unknown' }}>
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /slot state unknown/i,
    );
    expect(screen.getByText(/not confirmed in the slot/i)).toBeInTheDocument();
    expect(screen.queryByText(/not inserted/i)).not.toBeInTheDocument();
  });

  it('reads "awaiting insert (pre-configured)" for a deliberate bind-to-empty', async () => {
    await openCard(
      <EmptySlotHoverCard binding={{ ...baseBinding, preConfigured: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/awaiting insert \(pre-configured\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/^not inserted$/i)).not.toBeInTheDocument();
  });

  it('reads "ran out — awaiting new roll" for the spent runout latch', async () => {
    await openCard(
      <EmptySlotHoverCard binding={{ ...baseBinding, spent: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/ran out — awaiting new roll/i)).toBeInTheDocument();
  });

  it('lets the runout latch win over pre-configured — it is the more actionable truth', async () => {
    await openCard(
      <EmptySlotHoverCard binding={{ ...baseBinding, spent: true, preConfigured: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/ran out — awaiting new roll/i)).toBeInTheDocument();
    expect(screen.queryByText(/awaiting insert/i)).not.toBeInTheDocument();
  });

  it('hides the clear verb when the caller supplies no handler', async () => {
    await openCard(
      <EmptySlotHoverCard binding={baseBinding}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.queryByRole('button', { name: /clear the slot binding/i })).not.toBeInTheDocument();
  });

  it('releases the binding only after the confirm dialog is accepted', async () => {
    const onClearSlot = vi.fn();
    await openCard(
      <EmptySlotHoverCard binding={baseBinding} onClearSlot={onClearSlot}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    fireEvent.click(screen.getByRole('button', { name: /clear the slot binding for Overture PETG - Black/i }));
    await waitFor(() => expect(screen.getByText(/clear this slot\?/i)).toBeInTheDocument());
    // Nothing has been released yet — the click only opened the dialog.
    expect(onClearSlot).not.toHaveBeenCalled();
    // The dialog explains that usage survives the release (no data is lost).
    expect(screen.getByText(/returns to inventory with its recorded usage intact/i)).toBeInTheDocument();

    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /^Clear slot$/i }));
    expect(onClearSlot).toHaveBeenCalledTimes(1);
  });

  it('does not release the binding when the confirm dialog is cancelled', async () => {
    const onClearSlot = vi.fn();
    await openCard(
      <EmptySlotHoverCard binding={baseBinding} onClearSlot={onClearSlot}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    fireEvent.click(screen.getByRole('button', { name: /clear the slot binding for/i }));
    await waitFor(() => expect(screen.getByText(/clear this slot\?/i)).toBeInTheDocument());
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    expect(onClearSlot).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText(/clear this slot\?/i)).not.toBeInTheDocument());
  });

  it('hides the clear verb on a SEATED slot — clearing a live roll is semantically void', async () => {
    // The pipeline re-derives a binding for whatever is physically in the slot,
    // so the row comes straight back (and the round trip mints phantom inventory
    // rows). The resolution for an unread tray is identification, not deletion.
    await openCard(
      <EmptySlotHoverCard
        kind="present"
        binding={{ ...baseBinding, presence: 'seated' }}
        onClearSlot={vi.fn()}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /spool present/i,
    );
    expect(screen.queryByRole('button', { name: /clear the slot binding/i })).not.toBeInTheDocument();
  });

  it('keeps the clear verb where the claim CAN be stale (asserted-empty and unknown)', async () => {
    for (const kind of ['physical', 'unknown'] as const) {
      const header = kind === 'physical' ? /empty/i : /slot state unknown/i;
      const { unmount } = await openCard(
        <EmptySlotHoverCard
          kind={kind}
          binding={{ ...baseBinding, presence: kind === 'physical' ? 'empty' : 'unknown' }}
          onClearSlot={vi.fn()}
        >
          <div>trigger</div>
        </EmptySlotHoverCard>,
        header,
      );
      expect(screen.getByRole('button', { name: /clear the slot binding/i })).toBeInTheDocument();
      unmount();
    }
  });
});

// W5a — the tagless counterpart of "Re-spool tag…". Only the caller knows
// whether the bound row carries a tag, so the card renders the verb purely on
// the presence of the handler.
describe('FilamentHoverCard "New roll…" verb (W5a)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it('renders the verb and invokes the handler when a tagless roll is bound', async () => {
    const onNewRoll = vi.fn();
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 140, material: 'PETG', brand: 'Overture', color_name: 'Black' },
          onNewRoll,
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);

    const button = await screen.findByRole('button', { name: /New roll/i });
    fireEvent.click(button);
    expect(onNewRoll).toHaveBeenCalledTimes(1);
  });

  it('omits the verb for a tagged roll (the caller passes no handler)', async () => {
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 140, material: 'PETG', brand: 'Overture', color_name: 'Black' },
          onRespoolTag: vi.fn(),
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);

    await screen.findByRole('button', { name: /Re-spool tag/i });
    expect(screen.queryByRole('button', { name: /New roll/i })).not.toBeInTheDocument();
  });
});

// WS11 R8 — the standing undo for a "Re-check slot" mint. The card renders the
// verb purely on the presence of the handler; PrintersPage derives that from the
// backend's `recheck_undo_available`, so the offer lapsing removes the verb.
describe('FilamentHoverCard "Restore previous roll" verb (WS11 R8)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it('renders the verb as a keyboard-operable button and invokes the handler', async () => {
    const onRestorePreviousRoll = vi.fn();
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 140, material: 'PETG', brand: 'Overture', color_name: 'Black' },
          onRestorePreviousRoll,
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);

    const button = await screen.findByRole('button', { name: 'Restore previous roll' });
    // An offer, not an interruption: rendering it must not move focus.
    expect(button).not.toHaveFocus();
    // In the tab order (a real button, no tabindex=-1) and operable by keyboard.
    expect(button).not.toHaveAttribute('tabindex');
    button.focus();
    fireEvent.keyDown(button, { key: 'Enter' });
    fireEvent.click(button);
    expect(onRestorePreviousRoll).toHaveBeenCalledTimes(1);
  });

  it('omits the verb when no undo offer stands', async () => {
    renderWithHover(
      <FilamentHoverCard
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 140, material: 'PETG', brand: 'Overture', color_name: 'Black' },
          onNewRoll: vi.fn(),
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);

    await screen.findByRole('button', { name: /New roll/i });
    expect(
      screen.queryByRole('button', { name: 'Restore previous roll' })
    ).not.toBeInTheDocument();
  });
});
