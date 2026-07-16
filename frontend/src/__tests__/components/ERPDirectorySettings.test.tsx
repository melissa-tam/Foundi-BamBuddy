/**
 * Tests for ERPDirectorySettings — the ERP identity-directory login section
 * beside the LDAP settings. Covers field rendering/loading, the write-only
 * password behaviour, the role -> group mapping editor, and the read-only,
 * server-derived active/not-configured status (there is no enable toggle).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { ERPDirectorySettings } from '../../components/ERPDirectorySettings';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseSettings = {
  erp_login_active: false,
  erp_db_host: 'erp.local',
  erp_db_port: 3306,
  erp_db_name: 'FoundiDB',
  erp_db_user: 'ro_user',
  erp_db_password: '',
  erp_db_ssl: false,
  erp_role_group_mapping: '{"ADMIN":"Administrators","EDITOR":"Operators","VIEWER":"Viewers"}',
};

function useSettings(overrides: Record<string, unknown> = {}) {
  server.use(
    http.get('/api/v1/settings/', () => HttpResponse.json({ ...baseSettings, ...overrides }))
  );
}

beforeEach(() => {
  useSettings();
});

describe('ERPDirectorySettings', () => {
  it('renders the section and loads saved connection values into the form', async () => {
    render(<ERPDirectorySettings />);

    expect(await screen.findByRole('heading', { name: /ERP Directory Login/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/Database Host/i)).toHaveValue('erp.local');
    expect(screen.getByLabelText(/^Port$/i)).toHaveValue(3306);
    expect(screen.getByLabelText(/Database Name/i)).toHaveValue('FoundiDB');
    expect(screen.getByLabelText(/Database User/i)).toHaveValue('ro_user');
    // Password field starts blank (write-only; the API never returns it).
    expect(screen.getByLabelText(/Database Password/i)).toHaveValue('');
  });

  it('prefills the role -> group mapping from the stored JSON', async () => {
    render(<ERPDirectorySettings />);

    await screen.findByRole('heading', { name: /ERP Directory Login/i });
    expect(screen.getByLabelText('BamBuddy group for ADMIN')).toHaveValue('Administrators');
    expect(screen.getByLabelText('BamBuddy group for EDITOR')).toHaveValue('Operators');
    expect(screen.getByLabelText('BamBuddy group for VIEWER')).toHaveValue('Viewers');
  });

  it('saves connection settings and role mapping, omitting the password when blank', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(captured);
      })
    );

    const user = userEvent.setup();
    render(<ERPDirectorySettings />);
    await screen.findByRole('heading', { name: /ERP Directory Login/i });

    // Change the EDITOR mapping, then save.
    await user.selectOptions(screen.getByLabelText('BamBuddy group for EDITOR'), 'Viewers');
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured!.erp_db_host).toBe('erp.local');
    expect(captured!.erp_db_port).toBe(3306);
    expect(captured!.erp_db_name).toBe('FoundiDB');
    expect(captured!.erp_db_user).toBe('ro_user');
    expect(captured!.erp_db_ssl).toBe(false);
    // Password NOT sent when the field is left blank (write-only secret).
    expect('erp_db_password' in captured!).toBe(false);
    // Role mapping serialized as JSON with the edited EDITOR value.
    expect(JSON.parse(captured!.erp_role_group_mapping as string)).toEqual({
      ADMIN: 'Administrators',
      EDITOR: 'Viewers',
      VIEWER: 'Viewers',
    });
  });

  it('sends the password only when the admin types a new one', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(captured);
      })
    );

    const user = userEvent.setup();
    render(<ERPDirectorySettings />);
    await screen.findByRole('heading', { name: /ERP Directory Login/i });

    await user.type(screen.getByLabelText(/Database Password/i), 'new-secret');
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured!.erp_db_password).toBe('new-secret');
  });

  it('toggles the SSL switch and persists it on save', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.put('/api/v1/settings/', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(captured);
      })
    );

    const user = userEvent.setup();
    render(<ERPDirectorySettings />);
    await screen.findByRole('heading', { name: /ERP Directory Login/i });

    await user.click(screen.getByRole('switch', { name: /Connect over TLS/i }));
    await user.click(screen.getByRole('button', { name: /^Save$/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured!.erp_db_ssl).toBe(true);
  });

  it('shows the active status text when ERP login is configured', async () => {
    useSettings({ erp_login_active: true });
    render(<ERPDirectorySettings />);

    // Status is conveyed as text (not color alone) for WCAG.
    expect(await screen.findByText(/ERP login: active/i)).toBeInTheDocument();
    expect(screen.getByText(/Operators can sign in with their ERP directory credentials/i)).toBeInTheDocument();
    // The old enable/disable toggle is gone: no such control exists.
    expect(screen.queryByRole('button', { name: /^(Enable|Disable)$/i })).toBeNull();
  });

  it('shows the not-configured status text when ERP login is inactive', async () => {
    useSettings({ erp_login_active: false });
    render(<ERPDirectorySettings />);

    expect(await screen.findByText(/ERP login: not configured/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^(Enable|Disable)$/i })).toBeNull();
  });
});
