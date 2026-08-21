/**
 * Tests for the FilamentHoverCard component.
 * Focuses on fill level display and Spoolman source indicator.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: 75 }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: null }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: 0 }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'spoolman' }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: 80, fillSource: 'inventory' }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={{ ...baseFilamentData, fillLevel: null, fillSource: 'spoolman' }}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={baseFilamentData} disabled>
          <div>trigger</div>
        </FilamentHoverCard>
      );

      vi.advanceTimersByTime(100);

      // Card should not be visible
      expect(screen.queryByText('PLA Basic')).not.toBeInTheDocument();
    });

    it('shows filament details on hover', async () => {
      renderWithHover(
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic" data={baseFilamentData}>
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
        <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" configureSlot={{ enabled: true, onConfigure: vi.fn() }}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" configureSlot={{ enabled: true, onConfigure }}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" onAssignSpool={onAssign}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" onAssignSpool={onAssign}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" onUnassignSpool={vi.fn()}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.queryByText(/still assigned/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /unassign/i })).not.toBeInTheDocument();
  });

  it('names the bound spool, its record id and the grams already charged to it', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={baseBinding}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={baseBinding}>
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
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" kind="present" binding={{ ...baseBinding, presence: 'seated' }}>
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /spool present/i,
    );
    expect(screen.getByText(/awaiting identification/i)).toBeInTheDocument();
    expect(screen.queryByText(/not inserted/i)).not.toBeInTheDocument();
  });

  it('states unconfirmed, not empty, when the slot presence is unknown', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" kind="unknown" binding={{ ...baseBinding, presence: 'unknown' }}>
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /slot state unknown/i,
    );
    expect(screen.getByText(/not confirmed in the slot/i)).toBeInTheDocument();
    expect(screen.queryByText(/not inserted/i)).not.toBeInTheDocument();
  });

  it('reads "awaiting insert (pre-configured)" for a deliberate bind-to-empty', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={{ ...baseBinding, preConfigured: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/awaiting insert \(pre-configured\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/^not inserted$/i)).not.toBeInTheDocument();
  });

  it('reads "ran out — awaiting new roll" for the spent runout latch', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={{ ...baseBinding, spent: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/ran out — awaiting new roll/i)).toBeInTheDocument();
  });

  it('lets the runout latch win over pre-configured — it is the more actionable truth', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={{ ...baseBinding, spent: true, preConfigured: true }}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.getByText(/ran out — awaiting new roll/i)).toBeInTheDocument();
    expect(screen.queryByText(/awaiting insert/i)).not.toBeInTheDocument();
  });

  it('hides the clear verb when the caller supplies no handler', async () => {
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={baseBinding}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );
    expect(screen.queryByRole('button', { name: /unassign Overture PETG - Black from this slot/i })).not.toBeInTheDocument();
  });

  it('releases the binding only after the confirm dialog is accepted', async () => {
    const onUnassignSpool = vi.fn();
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={baseBinding} onUnassignSpool={onUnassignSpool}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    fireEvent.click(screen.getByRole('button', { name: /unassign Overture PETG - Black from this slot/i }));
    await waitFor(() => expect(screen.getByText(/unassign this spool\?/i)).toBeInTheDocument());
    // Nothing has been released yet — the click only opened the dialog.
    expect(onUnassignSpool).not.toHaveBeenCalled();
    // The dialog explains that usage survives the release (no data is lost).
    expect(screen.getByText(/returns to inventory with its recorded usage intact/i)).toBeInTheDocument();

    // The hover card is a (non-modal) dialog as well now, so the confirm is
    // addressed by its own accessible name rather than by being the only one.
    const dialog = screen.getByRole('dialog', { name: /unassign this spool/i });
    fireEvent.click(within(dialog).getByRole('button', { name: /^Unassign$/i }));
    expect(onUnassignSpool).toHaveBeenCalledTimes(1);
  });

  it('does not release the binding when the confirm dialog is cancelled', async () => {
    const onUnassignSpool = vi.fn();
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty" binding={baseBinding} onUnassignSpool={onUnassignSpool}>
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    fireEvent.click(screen.getByRole('button', { name: /unassign Overture PETG - Black from this slot/i }));
    await waitFor(() => expect(screen.getByText(/unassign this spool\?/i)).toBeInTheDocument());
    const dialog = screen.getByRole('dialog', { name: /unassign this spool/i });
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }));

    expect(onUnassignSpool).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByText(/unassign this spool\?/i)).not.toBeInTheDocument());
  });

  it('hides the clear verb on a SEATED slot — clearing a live roll is semantically void', async () => {
    // The pipeline re-derives a binding for whatever is physically in the slot,
    // so the row comes straight back (and the round trip mints phantom inventory
    // rows). The resolution for an unread tray is identification, not deletion.
    await openCard(
      <EmptySlotHoverCard label="AMS-A slot 1: Empty"
        kind="present"
        binding={{ ...baseBinding, presence: 'seated' }}
        onUnassignSpool={vi.fn()}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>,
      /spool present/i,
    );
    expect(screen.queryByRole('button', { name: /unassign Overture PETG - Black from this slot/i })).not.toBeInTheDocument();
  });

  it('keeps the clear verb where the claim CAN be stale (asserted-empty and unknown)', async () => {
    for (const kind of ['physical', 'unknown'] as const) {
      const header = kind === 'physical' ? /empty/i : /slot state unknown/i;
      const { unmount } = await openCard(
        <EmptySlotHoverCard label="AMS-A slot 1: Empty"
          kind={kind}
          binding={{ ...baseBinding, presence: kind === 'physical' ? 'empty' : 'unknown' }}
          onUnassignSpool={vi.fn()}
        >
          <div>trigger</div>
        </EmptySlotHoverCard>,
        header,
      );
      expect(screen.getByRole('button', { name: /unassign Overture PETG - Black from this slot/i })).toBeInTheDocument();
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
      <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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

  it('offers exactly ONE verb for a tagged roll too — no separate "Re-spool tag…"', async () => {
    // B4: the two verbs merged. Tag-ness changes the ledger lane the backend takes,
    // not the question the operator is answering, so the card offers one button.
    const onNewRoll = vi.fn();
    renderWithHover(
      <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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

    expect(await screen.findAllByRole('button', { name: /New roll/i })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /Re-spool tag/i })).not.toBeInTheDocument();
  });

  it('omits the verb when nothing is bound (the caller passes no handler)', async () => {
    renderWithHover(
      <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
        data={baseFilamentData}
        inventory={{ onAssignSpool: vi.fn() }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );
    vi.advanceTimersByTime(100);

    await screen.findByRole('button', { name: /Assign spool/i });
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

  // Replaces a test that CALLED itself keyboard-operable while opening the card
  // with `mouseEnter` and satisfying itself with a `fireEvent.click` after a
  // stray `keyDown` — it could not have failed if the card were mouse-only,
  // which is exactly what it was. This one never touches the pointer.
  it('reaches "Restore previous roll" by keyboard alone', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    const onRestorePreviousRoll = vi.fn();
    render(
      <FilamentHoverCard
        label="AMS-A slot 2: PETG"
        data={baseFilamentData}
        inventory={{
          assignedSpool: { id: 140, material: 'PETG', brand: 'Overture', color_name: 'Black' },
          onRestorePreviousRoll,
        }}
      >
        <div>trigger</div>
      </FilamentHoverCard>
    );

    // The slot is a tab stop that OFFERS its card on focus without taking the
    // keyboard hostage.
    await user.tab();
    const trigger = screen.getByRole('button', { name: 'AMS-A slot 2: PETG' });
    expect(trigger).toHaveFocus();
    const card = await screen.findByRole('dialog', { name: 'AMS-A slot 2: PETG' });
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(trigger).toHaveAttribute('aria-controls', card.id);

    // Enter hands the keyboard to the card's first control.
    await user.keyboard('{Enter}');
    await waitFor(() => expect(card.contains(document.activeElement)).toBe(true));

    const undo = within(card).getByRole('button', { name: 'Restore previous roll' });
    for (let i = 0; i < 10 && document.activeElement !== undo; i += 1) {
      await user.tab();
    }
    expect(undo).toHaveFocus();

    await user.keyboard('{Enter}');
    expect(onRestorePreviousRoll).toHaveBeenCalledTimes(1);

    // Dismissible from the keyboard, and the focus comes back to the slot it
    // came from (WCAG 2.2 1.4.13).
    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'AMS-A slot 2: PETG' })).not.toBeInTheDocument(),
    );
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes a HOVER-opened card on Escape without a click anywhere', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    render(
      <FilamentHoverCard label="AMS-A slot 2: PETG" data={baseFilamentData}>
        <div>trigger</div>
      </FilamentHoverCard>
    );

    fireEvent.mouseEnter(screen.getByTestId('filament-slot'));
    await screen.findByRole('dialog', { name: 'AMS-A slot 2: PETG' });

    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'AMS-A slot 2: PETG' })).not.toBeInTheDocument(),
    );
  });

  it('omits the verb when no undo offer stands', async () => {
    renderWithHover(
      <FilamentHoverCard label="AMS-A slot 1: PLA Basic"
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

// The empty-slot card's verbs have the same problem and the same fix: "Clear
// slot" is only reachable through the popover, so the popover has to be
// reachable without a pointer.
describe('EmptySlotHoverCard keyboard reach (WS11 B3)', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  const boundToEmptySlot = {
    spoolId: 140,
    label: 'Overture PETG - Black',
    usedGrams: 820,
    spent: false,
    preConfigured: false,
    presence: 'empty' as const,
  };

  it('reaches "Unassign" by keyboard alone', async () => {
    const user = userEvent.setup();
    const onUnassignSpool = vi.fn();
    render(
      <EmptySlotHoverCard
        label="AMS-A slot 3: Empty"
        binding={boundToEmptySlot}
        onUnassignSpool={onUnassignSpool}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    await user.tab();
    const trigger = screen.getByRole('button', { name: 'AMS-A slot 3: Empty' });
    expect(trigger).toHaveFocus();
    const card = await screen.findByRole('dialog', { name: 'AMS-A slot 3: Empty' });

    await user.keyboard('{Enter}');
    const clear = within(card).getByRole('button', {
      name: /unassign Overture PETG - Black from this slot/i,
    });
    await waitFor(() => expect(clear).toHaveFocus());

    // Activating it opens the confirm dialog — the release is still gated.
    await user.keyboard('{Enter}');
    const confirm = await screen.findByRole('dialog', { name: /unassign this spool/i });
    expect(onUnassignSpool).not.toHaveBeenCalled();

    await user.click(within(confirm).getByRole('button', { name: /^Unassign$/i }));
    expect(onUnassignSpool).toHaveBeenCalledTimes(1);
  });

  it('leaves Escape to the modal the card opened', async () => {
    const user = userEvent.setup();
    const onUnassignSpool = vi.fn();
    render(
      <EmptySlotHoverCard
        label="AMS-A slot 3: Empty"
        binding={boundToEmptySlot}
        onUnassignSpool={onUnassignSpool}
      >
        <div>trigger</div>
      </EmptySlotHoverCard>
    );

    await user.tab();
    const card = await screen.findByRole('dialog', { name: 'AMS-A slot 3: Empty' });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(card.contains(document.activeElement)).toBe(true));
    await user.keyboard('{Enter}');
    await screen.findByRole('dialog', { name: /unassign this spool/i });

    // The popover's own Escape handler must not eat the modal's dismissal.
    await user.keyboard('{Escape}');
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /unassign this spool/i })).not.toBeInTheDocument(),
    );
    expect(onUnassignSpool).not.toHaveBeenCalled();
  });

  it('leaves the card by Tab instead of trapping the keyboard inside it', async () => {
    const user = userEvent.setup();
    render(
      <>
        <EmptySlotHoverCard label="AMS-A slot 3: Empty" configureSlot={{ enabled: true, onConfigure: vi.fn() }}>
          <div>trigger</div>
        </EmptySlotHoverCard>
        <button type="button">next slot</button>
      </>
    );

    await user.tab();
    const card = await screen.findByRole('dialog', { name: 'AMS-A slot 3: Empty' });
    await user.keyboard('{Enter}');
    await waitFor(() => expect(card.contains(document.activeElement)).toBe(true));

    // Past the last control the popover gets out of the way — it is not modal,
    // so it must not hold the Tab cycle (and the portal would otherwise drop
    // the keyboard out of the page entirely).
    await user.tab();
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'AMS-A slot 3: Empty' })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('button', { name: 'next slot' })).toHaveFocus();
  });
});
