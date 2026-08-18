/**
 * Frontend tests for the Eject Profiles page (farm auto part-removal, Phase 1).
 *
 * Coverage:
 * - List renders profile rows.
 * - Empty state shows the "no profiles yet" copy + create CTA.
 * - Error state shows the load-error message + retry button.
 * - Create dialog blocks submission with a validation error when the name is
 *   blank, and creates a profile (and refreshes the list) on a valid submit.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EjectProfilesPage } from '../../pages/EjectProfilesPage';

function profile(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 1,
    name: 'Fast sweep',
    cooldown_temp_c: 28,
    clearance_mm: 10,
    z_offset_mm: 0.4,
    descent_steps: 4,
    x_passes: 11,
    x_margin_mm: 3,
    front_overhang_mm: 2,
    back_overhang_mm: 2,
    eject_speed_mm_min: 3000,
    skim_speed_mm_min: 1500,
    cooling_fan_assist: true,
    final_skim: true,
    max_part_height_mm: 42,
    sweep_x_min_mm: null,
    sweep_x_max_mm: null,
    sweep_start_frac: 1,
    bed_drop_clearance_mm: null,
    bed_drop_dwell_s: null,
    bed_drop_jitter_cycles: null,
    bed_drop_jitter_mm: null,
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-01T10:00:00Z',
    ...overrides,
  };
}

// The preview panel lists 3MF library files; stub the endpoint so the query
// doesn't fall through to the network whenever the list is non-empty.
const emptyLibraryFiles = http.get('*/api/v1/library/files', () => HttpResponse.json([]));

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

describe('EjectProfilesPage', () => {
  it('renders the list of eject profiles', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([profile({ name: 'Fast sweep' })])),
      emptyLibraryFiles,
    );

    render(<EjectProfilesPage />);

    expect(await screen.findByText('Fast sweep')).toBeInTheDocument();
    // A representative cell value (eject speed) from the row.
    expect(screen.getByText('3000')).toBeInTheDocument();
  });

  it('shows the empty state with a create CTA when there are no profiles', async () => {
    server.use(http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])));

    render(<EjectProfilesPage />);

    expect(await screen.findByText('No eject profiles yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new profile/i })).toBeInTheDocument();
  });

  it('shows the error state with a retry button when the list fails to load', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () => new HttpResponse(null, { status: 500 })),
    );

    render(<EjectProfilesPage />);

    expect(await screen.findByText(/could not load eject profiles/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('blocks the create dialog submit when the name is blank', async () => {
    let postCalled = false;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.post('*/api/v1/eject-profiles', () => {
        postCalled = true;
        return HttpResponse.json(profile(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('No eject profiles yet');
    await user.click(screen.getByRole('button', { name: /new profile/i }));

    // Dialog is open; submit with the (blank) name field.
    const dialog = await screen.findByRole('dialog');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
    expect(postCalled).toBe(false);
  });

  it('creates a profile and refreshes the list on a valid submit', async () => {
    let created = false;
    server.use(
      http.get('*/api/v1/eject-profiles', () =>
        HttpResponse.json(created ? [profile({ id: 7, name: 'Gentle sweep' })] : []),
      ),
      emptyLibraryFiles,
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toMatchObject({ name: 'Gentle sweep', cooldown_temp_c: 28 });
        created = true;
        return HttpResponse.json(profile({ id: 7, name: 'Gentle sweep' }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('No eject profiles yet');
    await user.click(screen.getByRole('button', { name: /new profile/i }));

    await screen.findByRole('dialog');
    await user.type(screen.getByLabelText(/^name$/i), 'Gentle sweep');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The row appears after the invalidated list re-fetches.
    expect(await screen.findByText('Gentle sweep')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('seeds the bed-drop toggle and clearance from an existing profile on edit', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () =>
        HttpResponse.json([profile({ id: 3, name: 'Drop sweep', bed_drop_clearance_mm: 50 })]),
      ),
      emptyLibraryFiles,
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('Drop sweep');
    await user.click(screen.getByRole('button', { name: /edit drop sweep/i }));

    await screen.findByRole('dialog');
    // The switch reflects the persisted (non-null) clearance, and the input is
    // seeded from the stored value rather than the prefill default.
    const dropSwitch = screen.getByRole('switch', { name: 'Bed-drop release assist' });
    expect(dropSwitch).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByLabelText('Bottom clearance (mm)')).toHaveValue(50);
  });

  it('hides the dwell and jitter controls until the bed-drop assist is enabled', async () => {
    server.use(http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])), emptyLibraryFiles);

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('No eject profiles yet');
    await user.click(screen.getByRole('button', { name: /new profile/i }));
    await screen.findByRole('dialog');

    // Both refinements live inside the bed-drop reveal — they only mean
    // anything while the drop itself runs.
    expect(screen.queryByRole('switch', { name: 'Hold at drop floor (s)' })).not.toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'Jitter at drop floor' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));

    expect(await screen.findByRole('switch', { name: 'Hold at drop floor (s)' })).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Jitter at drop floor' })).toBeInTheDocument();
    // Each refinement's inputs stay behind its own toggle.
    expect(screen.queryByRole('spinbutton', { name: 'Hold at drop floor (s)' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Jitter cycles')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Jitter travel (mm)')).not.toBeInTheDocument();
  });

  it('sends the entered dwell seconds and null jitter when only the dwell is enabled', async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      emptyLibraryFiles,
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(profile({ id: 11, name: 'dwell only' }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('No eject profiles yet');
    await user.click(screen.getByRole('button', { name: /new profile/i }));
    await screen.findByRole('dialog');

    await user.type(screen.getByLabelText('Name'), 'dwell only');
    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));
    await user.click(await screen.findByRole('switch', { name: 'Hold at drop floor (s)' }));

    const dwell = await screen.findByRole('spinbutton', { name: 'Hold at drop floor (s)' });
    await user.clear(dwell);
    await user.type(dwell, '12');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({
      bed_drop_clearance_mm: 50,
      bed_drop_dwell_s: 12,
      bed_drop_jitter_cycles: null,
      bed_drop_jitter_mm: null,
    });
  });

  it('seeds jitter from an existing profile and clears all three when the assist is switched off', async () => {
    let putBody: Record<string, unknown> | null = null;
    const stored = profile({
      id: 4,
      name: 'Jitter sweep',
      bed_drop_clearance_mm: 40,
      bed_drop_dwell_s: 7,
      bed_drop_jitter_cycles: 4,
      bed_drop_jitter_mm: 12,
    });
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([stored])),
      emptyLibraryFiles,
      http.put('*/api/v1/eject-profiles/:id', async ({ request }) => {
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(stored);
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('Jitter sweep');
    await user.click(screen.getByRole('button', { name: /edit jitter sweep/i }));
    await screen.findByRole('dialog');

    // Persisted values seed both refinements, not the UX prefills.
    expect(screen.getByRole('switch', { name: 'Jitter at drop floor' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('spinbutton', { name: 'Hold at drop floor (s)' })).toHaveValue(7);
    expect(screen.getByLabelText('Jitter cycles')).toHaveValue(4);
    expect(screen.getByLabelText('Jitter travel (mm)')).toHaveValue(12);

    // Turning the drop off clears the drop AND both refinements — a dwell or a
    // jitter without a drop would be meaningless state on the row.
    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody).toMatchObject({
      bed_drop_clearance_mm: null,
      bed_drop_dwell_s: null,
      bed_drop_jitter_cycles: null,
      bed_drop_jitter_mm: null,
    });
  });

  it('treats jitter as a both-or-neither pair', async () => {
    let putBody: Record<string, unknown> | null = null;
    // A half-configured row (cycles without travel) is NOT a configured jitter.
    const stored = profile({
      id: 5,
      name: 'Half jitter',
      bed_drop_clearance_mm: 40,
      bed_drop_jitter_cycles: 4,
      bed_drop_jitter_mm: null,
    });
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([stored])),
      emptyLibraryFiles,
      http.put('*/api/v1/eject-profiles/:id', async ({ request }) => {
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(stored);
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('Half jitter');
    await user.click(screen.getByRole('button', { name: /edit half jitter/i }));
    await screen.findByRole('dialog');

    const jitterSwitch = screen.getByRole('switch', { name: 'Jitter at drop floor' });
    expect(jitterSwitch).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByLabelText('Jitter cycles')).not.toBeInTheDocument();

    // Enabling it always yields a complete pair: the stored half seeds its own
    // input, the missing half takes the UX prefill.
    await user.click(jitterSwitch);
    expect(await screen.findByLabelText('Jitter cycles')).toHaveValue(4);
    expect(screen.getByLabelText('Jitter travel (mm)')).toHaveValue(10);

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(putBody).not.toBeNull());
    expect(putBody).toMatchObject({ bed_drop_jitter_cycles: 4, bed_drop_jitter_mm: 10 });
  });
});
