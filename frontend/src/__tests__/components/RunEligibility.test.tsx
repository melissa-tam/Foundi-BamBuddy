/**
 * The run eligibility panel — ONE component for the run detail page and the
 * runs-list card.
 *
 * What is pinned here is the pair of things that differ between those two
 * surfaces and nothing else: the heading LEVEL (so neither page's outline
 * breaks) and the CHROME (so a card never lands inside a card). Plus the
 * self-hide, which is what keeps a clean run's card from growing.
 */
import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import { NotEligibleBanner } from '../../components/RunEligibility';
import type { RunPrinterState } from '../../types/productionRuns';

function printerState(overrides: Partial<RunPrinterState> = {}): RunPrinterState {
  return {
    printer_id: 1,
    name: 'H2S-Alpha',
    connected: true,
    quarantined: false,
    awaiting_plate_clear: false,
    model_mismatch: false,
    model_mismatch_reason: null,
    stalled: false,
    vision_hold: false,
    filament_short_live: false,
    filament_short_detail: null,
    no_usb_drive: false,
    capability_reason: null,
    ...overrides,
  };
}

/** Card's own wrapper class — the one marker that a Card was rendered. */
const CARD_CLASS = 'bg-bambu-dark-secondary';

describe('NotEligibleBanner', () => {
  it('renders nothing when every printer is eligible', () => {
    const { container } = render(
      <NotEligibleBanner printerStates={[printerState()]} headingLevel={2} chrome="card" />,
    );
    // Not an empty-state card: a clean run adds no height at all.
    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
    expect(container.querySelector(`.${CARD_CLASS}`)).toBeNull();
  });

  it('renders nothing for an empty printer list', () => {
    const { container } = render(
      <NotEligibleBanner printerStates={[]} headingLevel={4} chrome="inline" />,
    );
    expect(screen.queryByRole('heading')).not.toBeInTheDocument();
    expect(container.querySelector('ul')).toBeNull();
  });

  it('lists each ineligible printer with its reasons', () => {
    render(
      <NotEligibleBanner
        printerStates={[
          printerState({ printer_id: 1, name: 'H2S-Alpha', connected: false }),
          printerState({ printer_id: 2, name: 'H2S-Beta' }),
          printerState({
            printer_id: 3,
            name: 'H2C-Gamma',
            quarantined: true,
            no_usb_drive: true,
            capability_reason: 'Needs a 0.6 nozzle; 0.4 mounted',
          }),
        ]}
        headingLevel={2}
        chrome="card"
      />,
    );

    expect(screen.getByText('H2S-Alpha')).toBeInTheDocument();
    expect(screen.getByText('Offline')).toBeInTheDocument();
    expect(screen.getByText('H2C-Gamma')).toBeInTheDocument();
    expect(screen.getByText('Quarantined')).toBeInTheDocument();
    expect(screen.getByText('No USB drive')).toBeInTheDocument();
    // A backend-authored capability sentence renders verbatim.
    expect(screen.getByText('Needs a 0.6 nozzle; 0.4 mounted')).toBeInTheDocument();
    // The eligible printer is absent — this is a blocked-printers list.
    expect(screen.queryByText('H2S-Beta')).not.toBeInTheDocument();
  });

  it('uses an h2 inside its own card on the detail page', () => {
    const { container } = render(
      <NotEligibleBanner
        printerStates={[printerState({ connected: false })]}
        headingLevel={2}
        chrome="card"
      />,
    );
    expect(screen.getByRole('heading', { level: 2, name: 'Printers not participating' })).toBeInTheDocument();
    expect(container.querySelector(`.${CARD_CLASS}`)).not.toBeNull();
  });

  it('uses an h4 and no card chrome inside a run card', () => {
    const { container } = render(
      <NotEligibleBanner
        printerStates={[printerState({ connected: false })]}
        headingLevel={4}
        chrome="inline"
      />,
    );
    // The run card's own title is an h3, so this heading must sit below it.
    expect(screen.getByRole('heading', { level: 4, name: 'Printers not participating' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument();
    // No Card-in-Card.
    expect(container.querySelector(`.${CARD_CLASS}`)).toBeNull();
  });
});
