/**
 * HoldChip — the printer card's one hold pill. The pill carries the noun; the
 * tooltip (what the hold asks for, or what the farm is doing) rides the
 * accessible InfoHint: a keyboard-reachable trigger whose accessible name is the
 * tooltip, revealed as `role="tooltip"` on focus — never a native `title`.
 * Copy is asserted through the `en` leaves.
 */
import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HoldChip } from '../../components/HoldChip';
import { holdChip } from '../../utils/incidentChip';
import type { HoldChipStatus } from '../../utils/incidentChip';
import en from '../../i18n/locales/en';

function renderChip(status: HoldChipStatus) {
  const chip = holdChip(status);
  if (chip === null) throw new Error('expected a chip');
  return render(<HoldChip chip={chip} />);
}

describe('HoldChip', () => {
  it('labels a farm load in flight "Toolhead empty" and names the slot in the tooltip', async () => {
    renderChip({
      state: 'PAUSE',
      open_incident: null,
      toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'loading', slot: 'AMS A slot 1', answer: null } },
    });
    const tooltip = en.printers.toolhead.loading.replace('{{slot}}', 'AMS A slot 1');

    expect(screen.getByText(en.printers.incident.toolhead_refill)).toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: tooltip });
    expect(trigger).not.toHaveAttribute('title');

    await userEvent.setup().tab();
    expect(trigger).toHaveFocus();
    expect(screen.getByRole('tooltip')).toHaveTextContent(tooltip);
  });

  it('names the failed slot, what the AMS answered and the exit', () => {
    renderChip({
      state: 'PAUSE',
      open_incident: null,
      toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'acted' } },
    });

    const tooltip = [
      en.printers.toolhead.loadFailed.replace('{{slot}}', 'AMS A slot 1'),
      en.printers.toolhead.answer.loadActed,
      en.printers.incidentAction.toolhead_refill,
    ].join(' ');
    expect(screen.getByRole('button', { name: tooltip })).toBeInTheDocument();
  });

  it('names a failed refill unload as an unload, never as "Load failed"', () => {
    renderChip({
      state: 'PAUSE',
      open_incident: null,
      toolhead: {
        feed: 'empty',
        active_tray: null,
        was_feeding_tray: null,
        refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'acted', command: 'unload' },
      },
    });

    const tooltip = [
      en.printers.toolhead.unloadFailed.replace('{{slot}}', 'AMS A slot 1'),
      en.printers.toolhead.answer.unloadActed,
      en.printers.incidentAction.toolhead_refill,
    ].join(' ');
    expect(screen.getByRole('button', { name: tooltip })).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: new RegExp(en.printers.toolhead.loadFailedAnySlot.replace('.', '')) }),
    ).not.toBeInTheDocument();
  });

  it('says what a Resume does when the backend verdict is owed', () => {
    renderChip({
      state: 'PAUSE',
      open_incident: null,
      toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: null, refill_reason: 'owed' },
    });

    expect(screen.getByText(en.printers.incident.toolhead_refill)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.printers.toolhead.reason.owed })).toBeInTheDocument();
  });

  it("ends an incident row's tooltip with the slot it names", () => {
    renderChip({
      state: 'PAUSE',
      toolhead: null,
      open_incident: {
        id: 9,
        kind: 'jam',
        status: 'escalated',
        driver_live: false,
        slot_desc: 'AMS A slot 3',
        created_at: null,
        operator_exits: false,
        printer_messages: [],
      },
    });

    expect(screen.getByText(en.printers.incident.jam)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `${en.printers.incidentAction.jam} — AMS A slot 3` }),
    ).toBeInTheDocument();
  });
});
