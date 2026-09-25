/**
 * THE `['shop-air']` query: `GET /shop-air`, the measured shop air and the eject
 * line derived from it — the same read the cooldown watch makes at arm, so the
 * readout can never show a line the farm is not using.
 *
 * Shop air moves slowly (at-rest samples land at most every 10 min per printer),
 * so a one-minute poll is plenty; the minute also keeps the readout's "N min
 * ago" honest. A settings save that changes the margin invalidates this key
 * (`SettingsPage`), so the line follows the margin without waiting for the poll.
 */
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

export const SHOP_AIR_QUERY_KEY = ['shop-air'] as const;
export const SHOP_AIR_REFETCH_MS = 60_000;

export function useShopAir() {
  return useQuery({
    queryKey: SHOP_AIR_QUERY_KEY,
    queryFn: api.getShopAir,
    refetchInterval: SHOP_AIR_REFETCH_MS,
  });
}
