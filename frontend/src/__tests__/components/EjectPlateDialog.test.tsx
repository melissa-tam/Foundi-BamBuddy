/**
 * `EjectPlateDialog` — the one dialog the eject flow opens, whatever door it
 * was entered by.
 *
 * What is pinned here is the operator-safety contract, not the markup: the
 * dialog has an accessible name keyed off WHICH plate this is, the sweep cannot
 * be confirmed without a part height (`max_z` sets the sweep clearance and
 * lift), the missing-height state is carried by text and a disabled control
 * rather than colour, and a confirm-leg failure is rendered in place instead of
 * as a toast that would vanish while the operator is reading the height they
 * have to correct.
 */

import { describe, it, expect, vi } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { EjectPlateDialog } from '../../components/EjectPlateDialog';
import type { EjectDialogState } from '../../hooks/useEjectPlate';
import type { EjectProfile } from '../../types/ejectProfiles';

const profiles = [
  { id: 7, name: 'Alpha profile' },
  { id: 9, name: 'Beta profile' },
] as EjectProfile[];

function dialogState(overrides: Partial<EjectDialogState> = {}): EjectDialogState {
  return {
    origin: 'foreign',
    printName: 'orphan_part.3mf',
    maxZHeightMm: 18,
    suggestedEjectProfileId: 9,
    declareOccupied: false,
    ...overrides,
  };
}

interface Overrides {
  dialog?: Partial<EjectDialogState>;
  heightInput?: string;
  heightValid?: boolean;
  error?: string | null;
  isPending?: boolean;
  ejectProfiles?: EjectProfile[];
  onConfirm?: () => void;
  onClose?: () => void;
  onSelectProfile?: (id: number | null) => void;
  onHeightChange?: (value: string) => void;
}

function mount(overrides: Overrides = {}) {
  const onConfirm = overrides.onConfirm ?? vi.fn();
  const onClose = overrides.onClose ?? vi.fn();
  const onSelectProfile = overrides.onSelectProfile ?? vi.fn();
  const onHeightChange = overrides.onHeightChange ?? vi.fn();
  render(
    <EjectPlateDialog
      printerId={4}
      dialog={dialogState(overrides.dialog)}
      ejectProfiles={overrides.ejectProfiles ?? profiles}
      selectedProfileId={9}
      onSelectProfile={onSelectProfile}
      heightInput={overrides.heightInput ?? '18'}
      onHeightChange={onHeightChange}
      heightValid={overrides.heightValid ?? true}
      error={overrides.error ?? null}
      isPending={overrides.isPending ?? false}
      onConfirm={onConfirm}
      onClose={onClose}
    />,
  );
  return { onConfirm, onClose, onSelectProfile, onHeightChange };
}

/** The dialog's confirm — distinct from the Cancel beside it. */
function confirmButton() {
  return within(screen.getByRole('dialog')).getByRole('button', { name: 'Eject now' });
}

describe('EjectPlateDialog — accessible name per origin', () => {
  it('names a plate the farm never dispatched', () => {
    mount();
    expect(
      screen.getByRole('dialog', { name: 'Eject plate — print not dispatched by the farm' }),
    ).toBeInTheDocument();
  });

  it('names the farm unit whose plate this is', () => {
    mount({ dialog: { origin: 'farm_unit', printName: 'SKU007.01 (#2656-20)' } });
    expect(
      screen.getByRole('dialog', { name: 'Eject plate — SKU007.01 (#2656-20)' }),
    ).toBeInTheDocument();
  });

  it('names an operator-declared plate without claiming an identity', () => {
    mount({ dialog: { origin: 'declared', printName: null } });
    expect(screen.getByRole('dialog', { name: 'Eject plate' })).toBeInTheDocument();
    // No print to name — the row says so rather than rendering an empty value.
    expect(screen.getByText('Unknown')).toBeInTheDocument();
  });

  it('states the check the operator owes before every sweep', () => {
    mount();
    expect(
      screen.getByText(
        'The sweep is built from the part height and the eject profile below. Check both against the plate before starting.',
      ),
    ).toBeInTheDocument();
  });
});

describe('EjectPlateDialog — the height gates the sweep', () => {
  it('disables the confirm and says why while the height is unknown', () => {
    mount({ dialog: { maxZHeightMm: null }, heightInput: '', heightValid: false });

    expect(confirmButton()).toBeDisabled();
    // Text, not colour: the reason is readable AND announced, because the
    // helper is linked to the field via aria-describedby.
    const helper = screen.getByText(
      'Part height unknown. Enter the tallest point of the part on the plate.',
    );
    expect(helper).toBeInTheDocument();
    expect(screen.getByLabelText('Part height (mm)')).toHaveAccessibleDescription(
      'Part height unknown. Enter the tallest point of the part on the plate.',
    );
  });

  it('enables the confirm once a usable height is present', async () => {
    const user = userEvent.setup();
    const { onConfirm } = mount();

    expect(screen.getByLabelText('Part height (mm)')).toHaveValue(18);
    expect(
      screen.queryByText('Part height unknown. Enter the tallest point of the part on the plate.'),
    ).not.toBeInTheDocument();

    await user.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('reports every keystroke so the confirm gate re-evaluates', async () => {
    const user = userEvent.setup();
    const { onHeightChange } = mount({ heightInput: '' });
    await user.type(screen.getByLabelText('Part height (mm)'), '2');
    expect(onHeightChange).toHaveBeenCalledWith('2');
  });

  it('disables the confirm when no eject profile can be chosen', () => {
    render(
      <EjectPlateDialog
        printerId={4}
        dialog={dialogState({ suggestedEjectProfileId: null })}
        ejectProfiles={[]}
        selectedProfileId={null}
        onSelectProfile={vi.fn()}
        heightInput="18"
        onHeightChange={vi.fn()}
        heightValid
        error={null}
        isPending={false}
        onConfirm={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(confirmButton()).toBeDisabled();
    expect(screen.getByText('No eject profiles available')).toBeInTheDocument();
  });
});

describe('EjectPlateDialog — profile picker', () => {
  it('preselects the backend suggestion and marks it as such', () => {
    mount();
    const select = screen.getByLabelText<HTMLSelectElement>('Eject profile');
    expect(select.value).toBe('9');
    expect(within(select).getByRole('option', { name: /Beta profile \(suggested\)/ })).toBeInTheDocument();
  });

  it('reports the operator override', async () => {
    const user = userEvent.setup();
    const { onSelectProfile } = mount();
    await user.selectOptions(screen.getByLabelText('Eject profile'), '7');
    expect(onSelectProfile).toHaveBeenCalledWith(7);
  });
});

describe('EjectPlateDialog — in-place failure and pending state', () => {
  it('renders a confirm-leg failure inside the dialog', () => {
    mount({ error: 'Part height 40.0 mm exceeds the profile limit of 10.0 mm' });
    const dialog = screen.getByRole('dialog');
    expect(
      within(dialog).getByText('Part height 40.0 mm exceeds the profile limit of 10.0 mm'),
    ).toBeInTheDocument();
    // The dialog stays usable so the operator can correct and retry.
    expect(confirmButton()).toBeEnabled();
  });

  it('locks every control while the eject is in flight', () => {
    mount({ isPending: true });
    expect(confirmButton()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByLabelText('Part height (mm)')).toBeDisabled();
    expect(screen.getByLabelText('Eject profile')).toBeDisabled();
  });

  it('closes on Cancel — the gate stays raised, which is the operator undo', async () => {
    const user = userEvent.setup();
    const { onClose } = mount();
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
