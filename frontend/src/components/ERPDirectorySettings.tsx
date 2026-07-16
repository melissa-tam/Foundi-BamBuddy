import { useState, useEffect } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Database, AlertTriangle, CheckCircle, Loader2 } from 'lucide-react';
import { api } from '../api/client';
import type { AppSettings } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

// The three ERP roles the identity directory reports. The mapping editor always
// renders one row per role, prefilled from the saved JSON; any extra keys in the
// stored mapping are preserved on save.
const ERP_ROLES = ['ADMIN', 'EDITOR', 'VIEWER'] as const;

interface ERPFormState {
  erp_db_host: string;
  erp_db_port: number;
  erp_db_name: string;
  erp_db_user: string;
  erp_db_password: string;
  erp_db_ssl: boolean;
  roleMapping: Record<string, string>;
}

function parseRoleMapping(raw: string | undefined): Record<string, string> {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const out: Record<string, string> = {};
      for (const [role, group] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof group === 'string') out[role] = group;
      }
      return out;
    }
  } catch {
    // Malformed JSON in storage — start from an empty mapping rather than throw.
  }
  return {};
}

export function ERPDirectorySettings() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [form, setForm] = useState<ERPFormState>({
    erp_db_host: '',
    erp_db_port: 3306,
    erp_db_name: '',
    erp_db_user: '',
    erp_db_password: '',
    erp_db_ssl: false,
    roleMapping: {},
  });

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.getSettings(),
  });

  const { data: groups = [] } = useQuery({
    queryKey: ['groups'],
    queryFn: () => api.getGroups(),
  });

  // Load saved settings into the form. The password is deliberately left blank —
  // the API never returns it, and an empty field means "keep the stored value".
  useEffect(() => {
    if (settings) {
      setForm({
        erp_db_host: settings.erp_db_host || '',
        erp_db_port: settings.erp_db_port ?? 3306,
        erp_db_name: settings.erp_db_name || '',
        erp_db_user: settings.erp_db_user || '',
        erp_db_password: '',
        erp_db_ssl: settings.erp_db_ssl ?? false,
        roleMapping: parseRoleMapping(settings.erp_role_group_mapping),
      });
    }
  }, [settings]);

  const saveMutation = useMutation({
    mutationFn: (data: Partial<AppSettings>) => api.updateSettings(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      showToast(t('settings.erp.settingsSaved'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const setRole = (role: string, group: string) =>
    setForm((prev) => ({ ...prev, roleMapping: { ...prev.roleMapping, [role]: group } }));

  const handleSave = () => {
    if (!form.erp_db_host) {
      showToast(t('settings.erp.errors.hostRequired'), 'error');
      return;
    }
    if (!form.erp_db_name) {
      showToast(t('settings.erp.errors.databaseRequired'), 'error');
      return;
    }

    const update: Record<string, unknown> = {
      erp_db_host: form.erp_db_host,
      erp_db_port: form.erp_db_port,
      erp_db_name: form.erp_db_name,
      erp_db_user: form.erp_db_user,
      erp_db_ssl: form.erp_db_ssl,
      erp_role_group_mapping: JSON.stringify(form.roleMapping),
    };
    // Write-only secret: only include the password when the admin typed a new one.
    if (form.erp_db_password) {
      update.erp_db_password = form.erp_db_password;
    }
    saveMutation.mutate(update as Partial<AppSettings>);
  };

  // ERP login has no on/off toggle: the server reports whether the connection
  // config resolves (deploy file or the DB overrides below). Read-only here.
  const active = settings?.erp_login_active ?? false;

  if (isLoading) {
    return (
      <div className="flex items-center justify-center p-12">
        <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
      </div>
    );
  }

  const inputClasses =
    'w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

  return (
    <div className="space-y-3">
      {/* Status (read-only, server-derived — no on/off toggle) */}
      <Card id="card-erp-status">
        <CardHeader>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Database className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">{t('settings.erp.title')}</h2>
            </div>
            {/* Text status (not color-only) so state is conveyed without relying on hue. */}
            <span
              role="status"
              className={`flex items-center gap-2 text-sm font-medium ${
                active ? 'text-green-400' : 'text-yellow-300'
              }`}
            >
              {active ? (
                <CheckCircle className="w-4 h-4 flex-shrink-0" aria-hidden="true" />
              ) : (
                <AlertTriangle className="w-4 h-4 flex-shrink-0" aria-hidden="true" />
              )}
              <span>{active ? t('settings.erp.statusActive') : t('settings.erp.statusInactive')}</span>
            </span>
          </div>
        </CardHeader>
        <CardContent>
          {active ? (
            <div className="bg-green-500/10 border border-green-500/30 rounded-lg p-4">
              <div className="flex items-start gap-3">
                <CheckCircle className="w-5 h-5 text-green-400 mt-0.5 flex-shrink-0" aria-hidden="true" />
                <div>
                  <p className="text-white font-medium">{t('settings.erp.activeDesc')}</p>
                  {/* Off affordance: ERP login turns off by clearing the connection config. */}
                  <p className="text-sm text-bambu-gray mt-1">{t('settings.erp.disableHint')}</p>
                </div>
              </div>
            </div>
          ) : (
            <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-4">
              <div className="flex items-start gap-3">
                <AlertTriangle className="w-5 h-5 text-yellow-400 mt-0.5 flex-shrink-0" aria-hidden="true" />
                <p className="text-white font-medium">{t('settings.erp.inactiveDesc')}</p>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Connection */}
      <Card id="card-erp-connection">
        <CardHeader>
          <h2 className="text-lg font-semibold text-white">{t('settings.erp.connectionConfig')}</h2>
        </CardHeader>
        <CardContent>
          <div className="space-y-3">
            {/* Host + Port */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div className="md:col-span-2">
                <label htmlFor="erp-host" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('settings.erp.host')}
                </label>
                <input
                  id="erp-host"
                  type="text"
                  className={inputClasses}
                  placeholder="erp.internal.example.com"
                  value={form.erp_db_host}
                  onChange={(e) => setForm({ ...form, erp_db_host: e.target.value })}
                />
              </div>
              <div>
                <label htmlFor="erp-port" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('settings.erp.port')}
                </label>
                <input
                  id="erp-port"
                  type="number"
                  min={1}
                  max={65535}
                  className={inputClasses}
                  value={form.erp_db_port}
                  onChange={(e) =>
                    setForm({ ...form, erp_db_port: e.target.value === '' ? 0 : Number(e.target.value) })
                  }
                />
              </div>
            </div>

            {/* Database + User */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label htmlFor="erp-db" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('settings.erp.database')}
                </label>
                <input
                  id="erp-db"
                  type="text"
                  className={inputClasses}
                  placeholder="Foundi_management_system"
                  value={form.erp_db_name}
                  onChange={(e) => setForm({ ...form, erp_db_name: e.target.value })}
                />
              </div>
              <div>
                <label htmlFor="erp-user" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('settings.erp.user')}
                </label>
                <input
                  id="erp-user"
                  type="text"
                  className={inputClasses}
                  placeholder="bambuddy_directory"
                  value={form.erp_db_user}
                  onChange={(e) => setForm({ ...form, erp_db_user: e.target.value })}
                />
              </div>
            </div>

            {/* Password + SSL */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label htmlFor="erp-password" className="block text-sm font-medium text-bambu-gray mb-1">
                  {t('settings.erp.password')}
                </label>
                <input
                  id="erp-password"
                  type="password"
                  className={inputClasses}
                  placeholder={settings?.erp_db_host ? '••••••••' : ''}
                  value={form.erp_db_password}
                  onChange={(e) => setForm({ ...form, erp_db_password: e.target.value })}
                />
                <p className="text-xs text-bambu-gray mt-1">{t('settings.erp.passwordHint')}</p>
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-3 cursor-pointer pb-2">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={form.erp_db_ssl}
                    aria-label={t('settings.erp.ssl')}
                    onClick={() => setForm({ ...form, erp_db_ssl: !form.erp_db_ssl })}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors flex-shrink-0 ${
                      form.erp_db_ssl ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                    }`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                        form.erp_db_ssl ? 'translate-x-6' : 'translate-x-1'
                      }`}
                    />
                  </button>
                  <div>
                    <span className="block text-sm font-medium text-white">{t('settings.erp.ssl')}</span>
                    <span className="block text-xs text-bambu-gray">{t('settings.erp.sslHint')}</span>
                  </div>
                </label>
              </div>
            </div>

            {/* Role -> Group mapping */}
            <div className="border-t border-bambu-dark-tertiary pt-3">
              <label className="block text-sm font-medium text-bambu-gray mb-1">
                {t('settings.erp.roleMapping')}
              </label>
              <p className="text-xs text-bambu-gray mb-2">{t('settings.erp.roleMappingHint')}</p>
              <div className="space-y-2">
                {ERP_ROLES.map((role) => (
                  <div key={role} className="flex items-center gap-2">
                    <span className="w-20 shrink-0 font-mono text-sm text-white">{role}</span>
                    <span className="text-bambu-gray text-sm shrink-0">→</span>
                    <select
                      className={inputClasses}
                      value={form.roleMapping[role] ?? ''}
                      aria-label={t('settings.erp.roleMappingGroupFor', { role })}
                      onChange={(e) => setRole(role, e.target.value)}
                    >
                      <option value="">{t('settings.erp.roleMappingNoGroup')}</option>
                      {groups.map((g) => (
                        <option key={g.id} value={g.name}>
                          {g.name}
                        </option>
                      ))}
                    </select>
                  </div>
                ))}
              </div>
            </div>

            {/* Save */}
            <div className="flex gap-3 pt-2">
              <Button onClick={handleSave} disabled={saveMutation.isPending}>
                {saveMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <CheckCircle className="w-4 h-4" />
                )}
                {t('common.save')}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
