import { useState, type ReactNode } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, Edit2, Trash2, Globe, Check, X, RefreshCw, ExternalLink, ImageOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import type { Group, OIDCProvider, OIDCProviderCreate } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Toggle } from './Toggle';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';

const EMPTY_FORM: OIDCProviderCreate = {
  name: '',
  issuer_url: '',
  client_id: '',
  client_secret: '',
  scopes: 'openid email profile',
  is_enabled: true,
  auto_create_users: false,
  auto_link_existing_accounts: false,
  email_claim: 'email',
  require_email_verified: true,
  icon_url: undefined,
  default_group_id: null,
  is_autologin: false,
  groups_claim: null,
  group_mapping: null,
};

interface MappingRow {
  claim: string;
  group: string;
}

// Convert a claim-value -> group-name map into ordered editor rows.
function mappingToRows(mapping: Record<string, string> | null | undefined): MappingRow[] {
  if (!mapping) return [];
  return Object.entries(mapping).map(([claim, group]) => ({ claim, group }));
}

// Collapse editor rows back to the API shape. Rows with a blank claim value are
// dropped; an empty result becomes null so the backend clears the stored mapping.
function rowsToMapping(rows: MappingRow[]): Record<string, string> | null {
  const out: Record<string, string> = {};
  for (const { claim, group } of rows) {
    const key = claim.trim();
    if (key && group) out[key] = group;
  }
  return Object.keys(out).length > 0 ? out : null;
}

// ─── Provider form (create / edit) ───────────────────────────────────────────
function ProviderForm({
  initial,
  isEdit = false,
  groups = [],
  onSave,
  onCancel,
  isPending,
}: {
  initial: OIDCProviderCreate;
  isEdit?: boolean;
  groups?: Group[];
  onSave: (data: OIDCProviderCreate) => void;
  onCancel: () => void;
  isPending: boolean;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<OIDCProviderCreate>(initial);
  const [secretChanged, setSecretChanged] = useState(false);
  const [mappingRows, setMappingRows] = useState<MappingRow[]>(() => mappingToRows(initial.group_mapping));
  const set = (key: keyof OIDCProviderCreate, value: unknown) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const updateRow = (index: number, patch: Partial<MappingRow>) =>
    setMappingRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const addRow = () => setMappingRows((prev) => [...prev, { claim: '', group: '' }]);
  const removeRow = (index: number) => setMappingRows((prev) => prev.filter((_, i) => i !== index));

  const inputCls =
    'w-full px-4 py-3 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white placeholder-bambu-gray focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors text-sm';
  const labelCls = 'block text-sm font-medium text-white mb-1';

  const handleSave = () => {
    const payload = { ...form };
    if (isEdit && !secretChanged) {
      delete (payload as Partial<OIDCProviderCreate>).client_secret;
    }
    // Normalize the group-sync fields: blank claim -> null (disables sync),
    // empty mapping -> null so the backend clears any stored mapping.
    const claim = (form.groups_claim ?? '').trim();
    payload.groups_claim = claim === '' ? null : claim;
    payload.group_mapping = rowsToMapping(mappingRows);
    onSave(payload);
  };

  const autoLinkOn = form.auto_link_existing_accounts === true;
  const emailVerifiedOn = form.require_email_verified ?? true;
  let requireEmailVerifiedDesc: ReactNode;
  if (autoLinkOn) {
    requireEmailVerifiedDesc = t('settings.oidc.form.requireEmailVerifiedAutoLink');
  } else if (emailVerifiedOn) {
    requireEmailVerifiedDesc = t('settings.oidc.form.requireEmailVerifiedDesc');
  } else {
    requireEmailVerifiedDesc = (
      <span className="text-red-400">{t('settings.oidc.form.requireEmailVerifiedWarning')}</span>
    );
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label className={labelCls}>{t('settings.oidc.form.name')} <span className="text-red-400">*</span></label>
          <input className={inputCls} value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="Google" />
        </div>
        <div>
          <label className={labelCls}>{t('settings.oidc.form.issuerUrl')} <span className="text-red-400">*</span></label>
          <input className={inputCls} value={form.issuer_url} onChange={(e) => set('issuer_url', e.target.value)} placeholder="https://accounts.google.com" />
        </div>
        <div>
          <label className={labelCls}>{t('settings.oidc.form.clientId')} <span className="text-red-400">*</span></label>
          <input className={inputCls} value={form.client_id} onChange={(e) => set('client_id', e.target.value)} placeholder="your-client-id" />
        </div>
        <div>
          <label className={labelCls}>
            {t('settings.oidc.form.clientSecret')}
            {!isEdit && <span className="text-red-400"> *</span>}
            {isEdit && <span className="text-bambu-gray text-xs ml-1">({t('settings.oidc.form.secretHint')})</span>}
          </label>
          <input
            className={inputCls}
            type="password"
            value={secretChanged ? form.client_secret : ''}
            placeholder={isEdit && !secretChanged ? '••••••••' : t('settings.oidc.form.secretPlaceholder')}
            onChange={(e) => {
              setSecretChanged(true);
              set('client_secret', e.target.value);
            }}
          />
        </div>
        <div>
          <label className={labelCls}>{t('settings.oidc.form.scopes')}</label>
          <input className={inputCls} value={form.scopes} onChange={(e) => set('scopes', e.target.value)} placeholder="openid email profile" />
        </div>
        <div>
          <label className={labelCls}>{t('settings.oidc.form.iconUrl')}</label>
          <input
            className={inputCls}
            value={form.icon_url ?? ''}
            onChange={(e) => set('icon_url', e.target.value === '' ? null : e.target.value)}
            placeholder="https://..."
          />
        </div>
      </div>

      <div className="flex flex-wrap gap-6 pt-2">
        <label className="flex items-center gap-3 cursor-pointer">
          <Toggle checked={form.is_enabled ?? true} onChange={(v) => set('is_enabled', v)} />
          <span className="text-white text-sm">{t('settings.oidc.form.enabled')}</span>
        </label>
        <label className="flex items-center gap-3 cursor-pointer">
          <Toggle checked={form.auto_create_users ?? false} onChange={(v) => set('auto_create_users', v)} />
          <div>
            <p className="text-white text-sm">{t('settings.oidc.form.autoCreate')}</p>
            <p className="text-bambu-gray text-xs">{t('settings.oidc.form.autoCreateDesc')}</p>
          </div>
        </label>
        <label className="flex items-center gap-3 cursor-pointer w-full">
          <Toggle checked={form.auto_link_existing_accounts ?? false} onChange={(v) => set('auto_link_existing_accounts', v)} />
          <div>
            <p className="text-white text-sm">{t('settings.oidc.form.autoLink')}</p>
            <p className="text-bambu-gray text-xs">{t('settings.oidc.form.autoLinkDesc')}</p>
          </div>
        </label>
        <label className="flex items-center gap-3 cursor-pointer w-full">
          <Toggle
            checked={emailVerifiedOn}
            onChange={(v) => set('require_email_verified', v)}
            disabled={autoLinkOn}
          />
          <div>
            <p className="text-white text-sm">{t('settings.oidc.form.requireEmailVerified')}</p>
            <p className="text-bambu-gray text-xs">{requireEmailVerifiedDesc}</p>
          </div>
        </label>
        <label className="flex items-center gap-3 cursor-pointer w-full">
          <Toggle checked={form.is_autologin ?? false} onChange={(v) => set('is_autologin', v)} />
          <div>
            <p className="text-white text-sm">{t('settings.oidc.form.autologin')}</p>
            <p className="text-bambu-gray text-xs">{t('settings.oidc.form.autologinDesc')}</p>
          </div>
        </label>
      </div>

      <div>
        <label className={labelCls}>{t('settings.oidc.form.emailClaim')}</label>
        <input
          className={inputCls}
          value={form.email_claim}
          onChange={(e) => set('email_claim', e.target.value || 'email')}
          placeholder={t('settings.oidc.form.emailClaimPlaceholder')}
        />
        <p className="text-bambu-gray text-xs mt-1">{t('settings.oidc.form.emailClaimDesc')}</p>
        {autoLinkOn && form.email_claim !== 'email' && (
          <p className="text-yellow-400 text-xs mt-1">{t('settings.oidc.form.emailClaimCustomClaimAutoLinkWarning')}</p>
        )}
      </div>

      <div>
        <label className={labelCls}>{t('settings.oidc.form.defaultGroup')}</label>
        <select
          className={inputCls}
          value={form.default_group_id ?? ''}
          onChange={(e) => set('default_group_id', e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t('settings.oidc.form.defaultGroupViewersFallback')}</option>
          {groups.map((g) => (
            <option key={g.id} value={g.id}>{g.name}</option>
          ))}
        </select>
        <p className="text-bambu-gray text-xs mt-1">{t('settings.oidc.form.defaultGroupDesc')}</p>
      </div>

      <div>
        <label className={labelCls}>{t('settings.oidc.form.groupsClaim')}</label>
        <input
          className={inputCls}
          value={form.groups_claim ?? ''}
          onChange={(e) => set('groups_claim', e.target.value)}
          placeholder={t('settings.oidc.form.groupsClaimPlaceholder')}
        />
        <p className="text-bambu-gray text-xs mt-1">{t('settings.oidc.form.groupsClaimDesc')}</p>
      </div>

      <div>
        <label className={labelCls}>{t('settings.oidc.form.groupMapping')}</label>
        <p className="text-bambu-gray text-xs mb-2">{t('settings.oidc.form.groupMappingDesc')}</p>
        <div className="space-y-2">
          {mappingRows.length === 0 && (
            <p className="text-bambu-gray text-xs italic">{t('settings.oidc.form.groupMappingNone')}</p>
          )}
          {mappingRows.map((row, index) => (
            <div key={index} className="flex items-center gap-2">
              <input
                className={inputCls}
                value={row.claim}
                onChange={(e) => updateRow(index, { claim: e.target.value })}
                placeholder={t('settings.oidc.form.groupMappingClaimValue')}
                aria-label={t('settings.oidc.form.groupMappingClaimValue')}
              />
              <span className="text-bambu-gray text-sm shrink-0">→</span>
              <select
                className={inputCls}
                value={row.group}
                onChange={(e) => updateRow(index, { group: e.target.value })}
                aria-label={t('settings.oidc.form.groupMappingGroup')}
              >
                <option value="">{t('settings.oidc.form.groupMappingSelectGroup')}</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.name}>{g.name}</option>
                ))}
              </select>
              <Button
                type="button"
                variant="secondary"
                size="sm"
                onClick={() => removeRow(index)}
                title={t('settings.oidc.form.groupMappingRemove')}
                aria-label={t('settings.oidc.form.groupMappingRemove')}
              >
                <Trash2 className="w-4 h-4" />
              </Button>
            </div>
          ))}
        </div>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          onClick={addRow}
          className="mt-2 inline-flex items-center gap-2"
        >
          <Plus className="w-4 h-4" />
          {t('settings.oidc.form.groupMappingAdd')}
        </Button>
      </div>

      <div className="flex gap-3 pt-2">
        <Button variant="secondary" onClick={onCancel} className="flex-1">
          {t('common.cancel')}
        </Button>
        <Button
          variant="primary"
          className="flex-1"
          disabled={!form.name || !form.issuer_url || !form.client_id || (!isEdit && !form.client_secret) || (isEdit && secretChanged && !form.client_secret) || isPending}
          onClick={handleSave}
        >
          {isPending ? t('common.saving') : t('common.save')}
        </Button>
      </div>
    </div>
  );
}

/**
 * Per-provider icon avatar in the admin Settings list (#1333 review).
 *
 * Extracted so each card has its own `iconFailed` state. Previously
 * `onError` just set `display: none` and the admin saw an unexplained gap
 * where the icon should be — now we swap in the Globe fallback exactly
 * like the `has_icon === false` branch, so the visual state is
 * self-explanatory regardless of why the icon didn't load.
 */
function ProviderIconAvatar({ provider }: { provider: OIDCProvider }) {
  const [iconFailed, setIconFailed] = useState(false);
  const showIcon = provider.has_icon && !iconFailed;
  if (showIcon) {
    return (
      <img
        src={api.oidcProviderIconUrl(provider.id)}
        alt={provider.name}
        className="w-8 h-8 rounded object-contain"
        onError={() => setIconFailed(true)}
      />
    );
  }
  return (
    <div className="w-8 h-8 rounded-full bg-bambu-dark-tertiary flex items-center justify-center">
      <Globe className="w-4 h-4 text-bambu-gray" />
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────
export function OIDCProviderSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [showCreate, setShowCreate] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<OIDCProvider | null>(null);

  const { data: providers, isLoading } = useQuery({
    queryKey: ['oidc-providers-all'],
    queryFn: () => api.getOIDCProvidersAll(),
  });

  const { data: groups = [] } = useQuery({
    queryKey: ['groups'],
    queryFn: () => api.getGroups(),
  });

  const createMutation = useMutation({
    mutationFn: (data: OIDCProviderCreate) => api.createOIDCProvider(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['oidc-providers-all'] });
      setShowCreate(false);
      showToast(t('settings.oidc.created'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<OIDCProviderCreate> }) =>
      api.updateOIDCProvider(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['oidc-providers-all'] });
      setEditingId(null);
      showToast(t('settings.oidc.updated'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteOIDCProvider(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['oidc-providers-all'] });
      setDeleteTarget(null);
      showToast(t('settings.oidc.deleted'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  // Icon-proxy mutations (#1333). Refresh re-fetches from the stored
  // icon_url; remove clears the cached bytes but keeps icon_url.
  const refreshIconMutation = useMutation({
    mutationFn: (id: number) => api.refreshOIDCProviderIcon(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['oidc-providers-all'] });
      queryClient.invalidateQueries({ queryKey: ['oidc-providers'] });
      showToast(t('settings.oidc.iconRefreshed'), 'success');
    },
    onError: (e: Error) => showToast(e.message || t('settings.oidc.iconFetchFailed'), 'error'),
  });

  const removeIconMutation = useMutation({
    mutationFn: (id: number) => api.deleteOIDCProviderIcon(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['oidc-providers-all'] });
      queryClient.invalidateQueries({ queryKey: ['oidc-providers'] });
      showToast(t('settings.oidc.iconRemoved'), 'success');
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const toggleEnabled = (provider: OIDCProvider) =>
    updateMutation.mutate({ id: provider.id, data: { is_enabled: !provider.is_enabled } });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <RefreshCw className="w-6 h-6 animate-spin text-bambu-green" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <Card id="card-oidc">
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-white font-semibold">{t('settings.oidc.title')}</h3>
              <p className="text-bambu-gray text-sm">{t('settings.oidc.desc')}</p>
            </div>
            {!showCreate && (
              <Button variant="primary" size="sm" onClick={() => setShowCreate(true)} className="flex items-center gap-2">
                <Plus className="w-4 h-4" />
                {t('settings.oidc.addProvider')}
              </Button>
            )}
          </div>
        </CardHeader>

        {showCreate && (
          <CardContent>
            <div className="border-t border-bambu-dark-tertiary pt-4">
              <h4 className="text-white font-medium mb-4">{t('settings.oidc.newProvider')}</h4>
              <ProviderForm
                initial={EMPTY_FORM}
                groups={groups}
                onSave={(data) => createMutation.mutate(data)}
                onCancel={() => setShowCreate(false)}
                isPending={createMutation.isPending}
              />
            </div>
          </CardContent>
        )}
      </Card>

      {/* Provider list */}
      {providers && providers.length === 0 && !showCreate && (
        <Card id="card-oidc-empty">
          <CardContent>
            <div className="text-center py-8 space-y-3">
              <Globe className="w-12 h-12 text-bambu-gray mx-auto" />
              <p className="text-bambu-gray">{t('settings.oidc.empty')}</p>
              <Button variant="primary" size="sm" onClick={() => setShowCreate(true)} className="inline-flex items-center gap-2">
                <Plus className="w-4 h-4" />
                {t('settings.oidc.addProvider')}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {providers?.map((provider) => (
        <Card key={provider.id}>
          <CardHeader>
            <div className="flex items-center gap-3">
              <ProviderIconAvatar provider={provider} />
              <div className="flex-1">
                <div className="flex items-center gap-2">
                  <h4 className="text-white font-medium">{provider.name}</h4>
                  {provider.is_enabled ? (
                    <span className="flex items-center gap-1 text-xs text-green-400 bg-green-400/10 px-2 py-0.5 rounded-full">
                      <Check className="w-3 h-3" /> {t('common.enabled')}
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 text-xs text-bambu-gray bg-bambu-dark-tertiary px-2 py-0.5 rounded-full">
                      <X className="w-3 h-3" /> {t('common.disabled')}
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-1 text-bambu-gray text-xs mt-0.5">
                  <ExternalLink className="w-3 h-3" />
                  <span>{provider.issuer_url}</span>
                </div>
              </div>
              <div className="flex items-center gap-2">
                {provider.icon_url && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => refreshIconMutation.mutate(provider.id)}
                    disabled={refreshIconMutation.isPending}
                    title={t('settings.oidc.refreshIcon')}
                    data-testid={`refresh-icon-${provider.id}`}
                  >
                    <RefreshCw className={`w-4 h-4 ${refreshIconMutation.isPending ? 'animate-spin' : ''}`} />
                  </Button>
                )}
                {provider.has_icon && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => removeIconMutation.mutate(provider.id)}
                    disabled={removeIconMutation.isPending}
                    title={t('settings.oidc.removeIcon')}
                    data-testid={`remove-icon-${provider.id}`}
                  >
                    <ImageOff className="w-4 h-4" />
                  </Button>
                )}
                <Toggle
                  checked={provider.is_enabled}
                  onChange={() => toggleEnabled(provider)}
                  disabled={updateMutation.isPending}
                />
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => setEditingId(editingId === provider.id ? null : provider.id)}
                >
                  <Edit2 className="w-4 h-4" />
                </Button>
                <Button variant="danger" size="sm" onClick={() => setDeleteTarget(provider)}>
                  <Trash2 className="w-4 h-4" />
                </Button>
              </div>
            </div>
          </CardHeader>

          {editingId === provider.id && (
            <CardContent>
              <div className="border-t border-bambu-dark-tertiary pt-4">
                <ProviderForm
                  isEdit={true}
                  groups={groups}
                  initial={{
                    name: provider.name,
                    issuer_url: provider.issuer_url,
                    client_id: provider.client_id,
                    client_secret: '',
                    scopes: provider.scopes,
                    is_enabled: provider.is_enabled,
                    auto_create_users: provider.auto_create_users,
                    auto_link_existing_accounts: provider.auto_link_existing_accounts,
                    email_claim: provider.email_claim,
                    require_email_verified: provider.require_email_verified,
                    icon_url: provider.icon_url ?? undefined,
                    default_group_id: provider.default_group_id ?? null,
                    is_autologin: provider.is_autologin,
                    groups_claim: provider.groups_claim ?? null,
                    group_mapping: provider.group_mapping ?? null,
                  }}
                  onSave={(data) => updateMutation.mutate({ id: provider.id, data })}
                  onCancel={() => setEditingId(null)}
                  isPending={updateMutation.isPending}
                />
              </div>
            </CardContent>
          )}

          {editingId !== provider.id && (
            <CardContent>
              <dl className="grid grid-cols-2 sm:grid-cols-3 gap-x-6 gap-y-2 text-sm">
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.clientId')}</dt>
                  <dd className="text-white font-mono truncate">{provider.client_id}</dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.scopes')}</dt>
                  <dd className="text-white">{provider.scopes}</dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.autoCreate')}</dt>
                  <dd className={provider.auto_create_users ? 'text-green-400' : 'text-bambu-gray'}>
                    {provider.auto_create_users ? t('common.yes') : t('common.no')}
                  </dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.autoLink')}</dt>
                  <dd className={provider.auto_link_existing_accounts ? 'text-green-400' : 'text-bambu-gray'}>
                    {provider.auto_link_existing_accounts ? t('common.yes') : t('common.no')}
                  </dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.emailClaim')}</dt>
                  <dd className="text-white font-mono">{provider.email_claim}</dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.requireEmailVerified')}</dt>
                  <dd className={provider.require_email_verified ? 'text-green-400' : 'text-red-400'}>
                    {provider.require_email_verified ? t('common.yes') : t('common.no')}
                  </dd>
                </div>
                <div>
                  <dt className="text-bambu-gray">{t('settings.oidc.form.defaultGroup')}</dt>
                  <dd className="text-white">
                    {provider.default_group_id
                      ? (groups.find((g) => g.id === provider.default_group_id)?.name ?? t('settings.oidc.form.defaultGroupViewersFallback'))
                      : t('settings.oidc.form.defaultGroupViewersFallback')}
                  </dd>
                </div>
                {provider.groups_claim && (
                  <div>
                    <dt className="text-bambu-gray">{t('settings.oidc.form.groupsClaim')}</dt>
                    <dd className="text-white font-mono">{provider.groups_claim}</dd>
                  </div>
                )}
                {provider.group_mapping && Object.keys(provider.group_mapping).length > 0 && (
                  <div className="col-span-2 sm:col-span-3">
                    <dt className="text-bambu-gray">{t('settings.oidc.form.groupMapping')}</dt>
                    <dd className="text-white">
                      <ul className="space-y-0.5">
                        {Object.entries(provider.group_mapping).map(([claim, group]) => (
                          <li key={claim} className="font-mono text-xs">
                            {claim} <span className="text-bambu-gray">→</span> {group}
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                )}
              </dl>
            </CardContent>
          )}
        </Card>
      ))}

      {/* Delete confirm */}
      {deleteTarget && (
        <ConfirmModal
          title={t('settings.oidc.deleteTitle')}
          message={t('settings.oidc.deleteMessage', { name: deleteTarget.name })}
          confirmText={t('common.delete')}
          variant="danger"
          onConfirm={() => deleteMutation.mutate(deleteTarget.id)}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
