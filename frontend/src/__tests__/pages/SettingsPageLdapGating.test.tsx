/**
 * The LDAP/ERP sub-tab under Settings → Users is admin-only, matching the
 * OIDC and Security sub-tabs. A non-admin (auth enabled, no session) never sees
 * the LDAP tab; an admin (auth disabled ⇒ full access in this app) does.
 */

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SettingsPage } from '../../pages/SettingsPage';

beforeEach(() => {
  // Land directly on the Users tab (the SettingsPage reads ?tab from the URL).
  window.history.pushState({}, '', '/settings?tab=users');
});

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
  window.history.pushState({}, '', '/');
});

describe('SettingsPage LDAP sub-tab gating', () => {
  it('shows the LDAP tab for an admin (auth disabled)', async () => {
    // Default handlers report auth_enabled:false ⇒ isAdmin is true.
    render(<SettingsPage />);

    // The Users sub-tab nav has rendered once the Email Authentication tab shows.
    await screen.findByRole('button', { name: /email authentication/i });
    expect(screen.getByRole('button', { name: /ldap/i })).toBeInTheDocument();
  });

  it('hides the LDAP tab for a non-admin (auth enabled, no session)', async () => {
    server.use(
      http.get('*/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false }),
      ),
    );

    render(<SettingsPage />);

    await screen.findByRole('button', { name: /email authentication/i });
    // OIDC/Security are already admin-only; LDAP now matches them.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /ldap/i })).not.toBeInTheDocument(),
    );
  });
});
