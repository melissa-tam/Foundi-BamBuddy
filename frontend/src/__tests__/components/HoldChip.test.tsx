/**
 * HoldChip — the printer card's one hold pill, rendered from the backend's verdict
 * (`PrinterStatus.hold_chip`, `services/hold_chip`). The pill carries the noun; the
 * tooltip (what the hold asks for, or what the farm is doing) rides the accessible
 * InfoHint: a keyboard-reachable trigger whose accessible name is the tooltip,
 * revealed as `role="tooltip"` on focus — never a native `title`. WHICH hold, its
 * tone and its words are the backend's (pinned in `test_hold_chip.py`); this file pins
 * that the card renders exactly what it is sent. Copy is asserted through the `en` leaves.
 */
import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HoldChip } from '../../components/HoldChip';
import type { HoldChipState } from '../../api/client';
import en from '../../i18n/locales/en';

const chip = (overrides: Partial<HoldChipState>): HoldChipState => ({
  variant: 'toolhead',
  tone: 'acting',
  label: { key: 'printers.incident.toolhead_refill', values: {} },
  tooltip: [],
  qualifier: null,
  ...overrides,
});

describe('HoldChip', () => {
  it('renders the noun on the pill and the tooltip on a focusable trigger, never a title', async () => {
    render(
      <HoldChip
        chip={chip({
          tooltip: [{ key: 'printers.toolhead.loading', values: { slot: 'AMS A slot 1' } }],
        })}
      />,
    );
    const tooltip = en.printers.toolhead.loading.replace('{{slot}}', 'AMS A slot 1');

    expect(screen.getByText(en.printers.incident.toolhead_refill)).toBeInTheDocument();
    const trigger = screen.getByRole('button', { name: tooltip });
    expect(trigger).not.toHaveAttribute('title');

    await userEvent.setup().tab();
    expect(trigger).toHaveFocus();
    expect(screen.getByRole('tooltip')).toHaveTextContent(tooltip);
  });

  it('joins the tooltip sentences in reading order', () => {
    render(
      <HoldChip
        chip={chip({
          tone: 'held',
          tooltip: [
            { key: 'printers.toolhead.loadFailed', values: { slot: 'AMS A slot 1' } },
            { key: 'printers.toolhead.answer.loadActed', values: {} },
            { key: 'printers.incidentAction.toolhead_refill', values: {} },
          ],
        })}
      />,
    );

    const tooltip = [
      en.printers.toolhead.loadFailed.replace('{{slot}}', 'AMS A slot 1'),
      en.printers.toolhead.answer.loadActed,
      en.printers.incidentAction.toolhead_refill,
    ].join(' ');
    expect(screen.getByRole('button', { name: tooltip })).toBeInTheDocument();
  });

  it('renders "Resuming" with the feeding slot and the time the resume went out', () => {
    render(
      <HoldChip
        chip={chip({
          label: { key: 'printers.toolhead.label.resuming', values: {} },
          tooltip: [
            { key: 'printers.toolhead.fedFrom', values: { slot: 'AMS A slot 2' } },
            { key: 'printers.toolhead.resumeSent', values: { time: '23:43' } },
          ],
        })}
      />,
    );

    expect(screen.getByText(en.printers.toolhead.label.resuming)).toBeInTheDocument();
    const tooltip = [
      en.printers.toolhead.fedFrom.replace('{{slot}}', 'AMS A slot 2'),
      en.printers.toolhead.resumeSent.replace('{{time}}', '23:43'),
    ].join(' ');
    expect(screen.getByRole('button', { name: tooltip })).toBeInTheDocument();
  });

  it("renders \"Resume refused\" with the printer's words and the exit", () => {
    render(
      <HoldChip
        chip={chip({
          variant: 'incident',
          tone: 'held',
          label: { key: 'printers.toolhead.label.resumeRefused', values: {} },
          tooltip: [
            { key: 'printers.toolhead.refusedBy', values: { reason: 'mqtt message verify failed' } },
            { key: 'printers.toolhead.pressResume', values: {} },
          ],
        })}
      />,
    );

    expect(screen.getByText(en.printers.toolhead.label.resumeRefused)).toBeInTheDocument();
    const tooltip = [
      en.printers.toolhead.refusedBy.replace('{{reason}}', 'mqtt message verify failed'),
      en.printers.toolhead.pressResume,
    ].join(' ');
    expect(screen.getByRole('button', { name: tooltip })).toBeInTheDocument();
  });

  it("ends an incident row's tooltip with the slot it names", () => {
    render(
      <HoldChip
        chip={chip({
          variant: 'incident',
          tone: 'held',
          label: { key: 'printers.incident.jam', values: {} },
          tooltip: [{ key: 'printers.incidentAction.jam', values: {} }],
          qualifier: 'AMS A slot 3',
        })}
      />,
    );

    expect(screen.getByText(en.printers.incident.jam)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `${en.printers.incidentAction.jam} — AMS A slot 3` }),
    ).toBeInTheDocument();
  });
});
