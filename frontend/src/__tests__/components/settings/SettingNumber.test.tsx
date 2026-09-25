/**
 * SettingNumber: the shared clamped Settings number field. The rule under
 * test is the emptied-field draft — a controlled number input with a
 * `|| fallback` cannot be typed into once cleared (with min 1, clearing and
 * typing "5" yields "15"; with a signed range a lone "-" is untypable). An
 * unparseable entry must therefore write NOTHING and stay on screen, while a
 * parseable one clamps and commits at once. The `decimal` kind also holds a
 * below-min entry as a keystroke ("0" begins "0.5") and clamps it on blur.
 */
import { useState } from 'react';
import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, cleanup, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { SettingNumber, type SettingNumberKind } from '../../../components/settings/SettingNumber';

/** Controlled host: the component owns the draft, the parent owns the value. */
function Harness({
  initial,
  min,
  max,
  enabled,
  kind,
  step,
  onChange,
}: {
  initial: number;
  min: number;
  max: number;
  enabled?: boolean;
  kind?: SettingNumberKind;
  step?: number;
  onChange?: (v: number) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SettingNumber
      id="probe"
      label="Aux fan speed (%)"
      hint="One speed for the whole cooldown."
      value={value}
      onChange={(v) => {
        setValue(v);
        onChange?.(v);
      }}
      min={min}
      max={max}
      enabled={enabled}
      kind={kind}
      step={step}
    />
  );
}

const field = () => screen.getByLabelText('Aux fan speed (%)') as HTMLInputElement;

describe('SettingNumber', () => {
  afterEach(cleanup);

  it('associates the visible label with the input and shows the stored value', () => {
    render(<Harness initial={100} min={1} max={100} />);

    expect(field().value).toBe('100');
    expect(field()).toHaveAttribute('min', '1');
    expect(field()).toHaveAttribute('max', '100');
  });

  it('clamps an over- and under-range entry to the bounds', () => {
    const onChange = vi.fn();
    render(<Harness initial={100} min={1} max={100} onChange={onChange} />);

    fireEvent.change(field(), { target: { value: '150' } });
    expect(field().value).toBe('100');
    expect(onChange).toHaveBeenLastCalledWith(100);

    fireEvent.change(field(), { target: { value: '-5' } });
    expect(field().value).toBe('1');
    expect(onChange).toHaveBeenLastCalledWith(1);
  });

  it('commits an in-range entry unchanged', () => {
    const onChange = vi.fn();
    render(<Harness initial={100} min={1} max={100} onChange={onChange} />);

    fireEvent.change(field(), { target: { value: '60' } });
    expect(field().value).toBe('60');
    expect(onChange).toHaveBeenCalledWith(60);
  });

  it('keeps an emptied field empty and writes no fallback value', () => {
    const onChange = vi.fn();
    render(<Harness initial={100} min={1} max={100} onChange={onChange} />);

    fireEvent.change(field(), { target: { value: '' } });
    // The stored value must NOT be substituted back into the box — that is the
    // bug that turns a cleared field into "15" on the next keystroke.
    expect(field().value).toBe('');
    expect(onChange).not.toHaveBeenCalled();

    // Typing on resumes normally from the emptied state.
    fireEvent.change(field(), { target: { value: '5' } });
    expect(field().value).toBe('5');
    expect(onChange).toHaveBeenCalledWith(5);
  });

  it('keeps a lone minus sign so a negative target can be typed', () => {
    const onChange = vi.fn();
    render(<Harness initial={100} min={-50} max={200} onChange={onChange} />);

    // A number input reports "-" as "".
    fireEvent.change(field(), { target: { value: '-' } });
    expect(field().value).not.toBe('100');
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.change(field(), { target: { value: '-35' } });
    expect(field().value).toBe('-35');
    expect(onChange).toHaveBeenCalledWith(-35);
  });

  it('drops the draft on blur and shows the stored value', () => {
    render(<Harness initial={100} min={1} max={100} />);

    fireEvent.change(field(), { target: { value: '' } });
    expect(field().value).toBe('');

    fireEvent.blur(field());
    expect(field().value).toBe('100');
  });

  it('disables the input when the governing switch is off', () => {
    render(<Harness initial={100} min={1} max={100} enabled={false} />);

    expect(field().disabled).toBe(true);
  });

  it('truncates a fractional entry in the default integer kind', () => {
    const onChange = vi.fn();
    render(<Harness initial={10} min={3} max={60} onChange={onChange} />);

    fireEvent.change(field(), { target: { value: '12.5' } });
    expect(onChange).toHaveBeenLastCalledWith(12);
  });

  describe('decimal kind', () => {
    it('commits a fractional entry unchanged', () => {
      const onChange = vi.fn();
      render(<Harness initial={2} min={0.5} max={10} step={0.5} kind="decimal" onChange={onChange} />);

      fireEvent.change(field(), { target: { value: '2.5' } });
      expect(field().value).toBe('2.5');
      expect(onChange).toHaveBeenLastCalledWith(2.5);
      expect(field()).toHaveAttribute('step', '0.5');
    });

    it('holds a below-min entry as a keystroke so "0.5" can be typed', () => {
      const onChange = vi.fn();
      render(<Harness initial={2} min={0.5} max={10} kind="decimal" onChange={onChange} />);

      // "0" is how "0.5" begins — clamping it at once would rewrite the box.
      fireEvent.change(field(), { target: { value: '0' } });
      expect(field().value).toBe('0');
      expect(onChange).not.toHaveBeenCalled();

      fireEvent.change(field(), { target: { value: '0.5' } });
      expect(field().value).toBe('0.5');
      expect(onChange).toHaveBeenLastCalledWith(0.5);
    });

    it('commits the floor when a below-min entry is left', () => {
      const onChange = vi.fn();
      render(<Harness initial={2} min={0.5} max={10} kind="decimal" onChange={onChange} />);

      fireEvent.change(field(), { target: { value: '0' } });
      fireEvent.blur(field());
      expect(onChange).toHaveBeenLastCalledWith(0.5);
      expect(field().value).toBe('0.5');
    });

    it('clamps an over-max entry at once', () => {
      const onChange = vi.fn();
      render(<Harness initial={2} min={0.5} max={10} kind="decimal" onChange={onChange} />);

      fireEvent.change(field(), { target: { value: '12.5' } });
      expect(field().value).toBe('10');
      expect(onChange).toHaveBeenLastCalledWith(10);
    });
  });

  it('carries the mechanism copy in a tooltip, never inline', async () => {
    const user = userEvent.setup();
    render(<Harness initial={100} min={1} max={100} />);

    expect(screen.queryByText('One speed for the whole cooldown.')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'One speed for the whole cooldown.' }));
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      'One speed for the whole cooldown.',
    );
  });
});
