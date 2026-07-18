/**
 * Field primitives — FormField accessibility wiring.
 *
 * Behaviour-first: exercise what an assistive-tech user relies on — the label
 * is associated with the control (clicking it focuses the control), and the
 * control's `aria-describedby` resolves to the help AND error nodes with
 * `aria-invalid` set only when an error is present.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { FormField, Input } from '../../components/ui/Field';

describe('FormField', () => {
  it('associates the label with the control (clicking the label focuses it)', async () => {
    const user = userEvent.setup();
    render(
      <FormField id="widget-name" label="Widget name">
        {(field) => <Input {...field} defaultValue="" />}
      </FormField>,
    );

    const input = screen.getByLabelText('Widget name');
    expect(input).not.toHaveFocus();

    await user.click(screen.getByText('Widget name'));
    expect(input).toHaveFocus();
  });

  it('wires aria-describedby to both the help and error ids and sets aria-invalid when errored', () => {
    render(
      <FormField id="qty" label="Quantity" help="Whole plates only" error="Quantity is required">
        {(field) => <Input {...field} />}
      </FormField>,
    );

    const input = screen.getByLabelText('Quantity');
    expect(input).toHaveAttribute('aria-describedby', 'qty-help qty-error');
    expect(input).toHaveAttribute('aria-invalid', 'true');

    // The described-by ids actually resolve to the rendered help + error nodes.
    const help = screen.getByText('Whole plates only');
    const error = screen.getByText('Quantity is required');
    expect(document.getElementById('qty-help')).toBe(help);
    expect(document.getElementById('qty-error')).toBe(error);
    // The error is announced.
    expect(error).toHaveAttribute('role', 'alert');
    expect(screen.getByRole('alert')).toBe(error);
  });

  it('describes with the help id alone and omits aria-invalid when there is no error', () => {
    render(
      <FormField id="notes" label="Notes" help="Optional context">
        {(field) => <Input {...field} />}
      </FormField>,
    );

    const input = screen.getByLabelText('Notes');
    expect(input).toHaveAttribute('aria-describedby', 'notes-help');
    expect(input).not.toHaveAttribute('aria-invalid');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('leaves the control undescribed when it has neither help nor error', () => {
    render(
      <FormField id="bare" label="Bare">
        {(field) => <Input {...field} />}
      </FormField>,
    );

    const input = screen.getByLabelText('Bare');
    expect(input).not.toHaveAttribute('aria-describedby');
    expect(input).not.toHaveAttribute('aria-invalid');
  });
});
