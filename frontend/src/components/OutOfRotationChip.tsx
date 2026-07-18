/**
 * OutOfRotationChip surfaces a spool that a feed-fault jam took out of dispatch
 * rotation (`Spool.feed_fault_at != null`; #feed-fault) and provides the single
 * control to clear the flag ("Return to rotation").
 *
 * Self-contained: it owns its mutation so it can be dropped into any surface
 * that already has the spool id in hand (InventoryPage rows/cards, the
 * PrintersPage AMS slot hover card) without threading callbacks. Clearing is
 * safe/reversible — the next feed-fault HMS re-stamps the flag server-side.
 *
 * Not colour-only: the status carries an AlertTriangle glyph plus a title that
 * reuses the long `ams.outOfRotation` tooltip copy (with the fault code
 * appended when present) for hover/keyboard discovery.
 *
 * Permission note: InventoryPage does not UI-gate its spool mutations (edit /
 * archive / delete) — the server enforces `inventory:update` on
 * PATCH /inventory/spools/{id}. The button here follows that neighbour and is
 * left ungated in the UI.
 */

import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, RotateCcw } from 'lucide-react';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';

interface OutOfRotationChipProps {
  spoolId: number;
  /** HMS / fault code that tripped the flag, shown in the status title. */
  faultCode?: string | null;
  /** Extra classes for the wrapping flex row (e.g. spacing in a hover card). */
  className?: string;
}

// Query keys refreshed after a spool returns to rotation. Prefix matching means
// ['spool-assignments'] also invalidates ['spool-assignments', printerId]. Over-
// invalidation is safe: these are cheap list/assignment reads, and covering both
// the inventory and printer surfaces lets the chip drop wherever it was shown.
const AFFECTED_QUERY_KEYS: readonly string[][] = [
  ['inventory-spools'],
  ['spoolman-inventory-spools'],
  ['inventory-locations'],
  ['spool-assignments'],
  ['spoolman-slot-assignments'],
  ['spoolman-slot-assignments-all'],
];

export function OutOfRotationChip({ spoolId, faultCode, className = '' }: OutOfRotationChipProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const returnMutation = useMutation({
    // Explicit null clears the flag: the backend's SpoolUpdate treats an
    // explicit-null feed_fault_at (exclude_unset) as the manual-clear signal.
    mutationFn: () => api.updateSpool(spoolId, { feed_fault_at: null }),
    onSuccess: () => {
      AFFECTED_QUERY_KEYS.forEach((queryKey) => {
        queryClient.invalidateQueries({ queryKey });
      });
      showToast(t('inventory.returnToRotationDone'), 'success');
    },
    onError: () => {
      showToast(t('inventory.returnToRotationFailed'), 'error');
    },
  });

  const statusTitle = faultCode
    ? `${t('ams.outOfRotation')} (${faultCode})`
    : t('ams.outOfRotation');

  return (
    <div className={`flex flex-wrap items-center gap-1.5 ${className}`}>
      <span
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-400/15 text-amber-300 border border-amber-400/30"
        title={statusTitle}
      >
        <AlertTriangle className="w-3 h-3" aria-hidden="true" strokeWidth={2.5} />
        {t('ams.outOfRotationShort')}
      </span>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          returnMutation.mutate();
        }}
        disabled={returnMutation.isPending}
        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium bg-bambu-green/20 hover:bg-bambu-green/30 text-bambu-green transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        title={t('inventory.returnToRotation')}
      >
        <RotateCcw
          className={`w-3 h-3 ${returnMutation.isPending ? 'animate-spin' : ''}`}
          aria-hidden="true"
        />
        {t('inventory.returnToRotation')}
      </button>
    </div>
  );
}
