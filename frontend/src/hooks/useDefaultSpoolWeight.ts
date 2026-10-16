import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

/**
 * The label weight (g) a new roll starts from — the `default_spool_weight_g`
 * setting, read off the shared `['settings']` query.
 *
 * The ONE frontend reader of the setting: the Add spool form default, the New
 * roll dialog's tagless prefill and the SpoolBuddy quick-add all ask here. The
 * backend owner (`services/spool_defaults`) applies the same setting to every
 * roll created with no positive figure, so a create sent before this value
 * resolves (a figure of 0) still lands on it. `undefined` until the settings
 * query resolves; never a client-side literal.
 */
export function useDefaultSpoolWeight(enabled = true): number | undefined {
  const { data } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    enabled,
    select: (settings) => settings.default_spool_weight_g,
  });
  return data;
}
