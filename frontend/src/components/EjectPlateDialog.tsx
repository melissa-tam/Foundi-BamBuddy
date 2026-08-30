/**
 * The one eject dialog, reached from all three doors on a printer card.
 *
 * The backend refuses the first eject call with `foreign_plate` whenever the
 * sweep needs operator input — a plate the farm never dispatched, a plate the
 * operator just declared occupied, or a farm unit whose donor the operator
 * should still eyeball. The title names which of those it is; the flow is the
 * same in all three: check the part height, pick the sweep profile, confirm.
 *
 * The part height is editable because the value parsed from the donor 3MF can
 * be wrong for a plate the farm did not dispatch, and absent entirely when
 * there is no donor to parse. `max_z` sets the sweep clearance and lift, so an
 * understated value risks sweep-path contact — the operator checks it against
 * the real part before confirming.
 *
 * Cancelling leaves the plate gate raised by design: on the declare leg the
 * server already raised it and the plate IS occupied. "Mark plate as cleared"
 * is the undo, not this dialog's Cancel.
 */

import { useTranslation } from 'react-i18next';
import { AlertTriangle, Loader2, Wind } from 'lucide-react';
import { Modal } from './ui/Modal';
import { FormField, Input } from './ui/Field';
import { InlineAlert } from './ui/InlineAlert';
import type { EjectDialogState } from '../hooks/useEjectPlate';
import type { EjectProfile } from '../types/ejectProfiles';

interface EjectPlateDialogProps {
  /** Scopes the element ids so two open cards cannot collide. */
  printerId: number;
  dialog: EjectDialogState;
  ejectProfiles: EjectProfile[];
  selectedProfileId: number | null;
  onSelectProfile: (id: number | null) => void;
  heightInput: string;
  onHeightChange: (value: string) => void;
  /** False while the height is blank or non-positive — blocks the confirm. */
  heightValid: boolean;
  /** Confirm-leg failure, rendered here rather than as a toast that would
   *  vanish while the operator is reading the height they must correct. */
  error: string | null;
  isPending: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function EjectPlateDialog({
  printerId,
  dialog,
  ejectProfiles,
  selectedProfileId,
  onSelectProfile,
  heightInput,
  onHeightChange,
  heightValid,
  error,
  isPending,
  onConfirm,
  onClose,
}: EjectPlateDialogProps) {
  const { t } = useTranslation();
  const titleId = `eject-dialog-title-${printerId}`;
  const profileId = `eject-dialog-profile-${printerId}`;
  const printName = dialog.printName || t('printers.eject.foreignUnknownPrint');

  const title =
    dialog.origin === 'farm_unit'
      ? t('printers.eject.dialog.farmUnitTitle', { name: printName })
      : dialog.origin === 'declared'
        ? t('printers.eject.dialog.declaredTitle')
        : t('printers.eject.dialog.foreignTitle');

  return (
    <Modal
      onClose={onClose}
      labelledBy={titleId}
      widthClass="max-w-md"
      closeOnOverlay={!isPending}
      dismissDisabled={isPending}
      className="p-5"
    >
      <div className="flex items-start gap-3 mb-4">
        <AlertTriangle className="w-5 h-5 text-yellow-400 flex-shrink-0 mt-0.5" />
        <div>
          <h3 id={titleId} className="text-sm font-semibold text-white mb-1">
            {title}
          </h3>
          <p className="text-xs text-bambu-gray leading-relaxed">
            {t('printers.eject.dialog.body')}
          </p>
        </div>
      </div>
      <dl className="mb-4 space-y-1 text-xs">
        <div className="flex justify-between gap-3">
          <dt className="text-bambu-gray flex-shrink-0">
            {t('printers.eject.foreignPrintNameLabel')}
          </dt>
          <dd className="text-white text-right break-all">{printName}</dd>
        </div>
      </dl>
      {/* The unknown-height state is carried by the helper text AND the disabled
          confirm — never by colour alone. `help` is linked via
          aria-describedby, so the reason reaches assistive tech too. */}
      <FormField
        id={`eject-dialog-height-${printerId}`}
        label={t('printers.eject.foreignPartHeightEditLabel')}
        labelClassName="block text-xs text-bambu-gray mb-1"
        className="mb-4"
        help={heightValid ? undefined : t('printers.eject.dialog.heightRequired')}
      >
        {(field) => (
          <div className="flex items-center gap-2">
            <Input
              {...field}
              type="number"
              step="0.1"
              min="0"
              value={heightInput}
              onChange={(e) => onHeightChange(e.target.value)}
              disabled={isPending}
              className="text-sm disabled:opacity-50"
            />
            <span className="text-xs text-bambu-gray flex-shrink-0">mm</span>
          </div>
        )}
      </FormField>
      <div className="mb-4">
        <label htmlFor={profileId} className="block text-xs text-bambu-gray mb-1">
          {t('printers.eject.foreignProfileLabel')}
        </label>
        <select
          id={profileId}
          value={selectedProfileId ?? ''}
          onChange={(e) => onSelectProfile(e.target.value ? Number(e.target.value) : null)}
          disabled={isPending || ejectProfiles.length === 0}
          className="w-full px-3 py-2 rounded-lg text-sm bg-bambu-dark border border-bambu-dark-tertiary text-white focus:outline-none focus:border-bambu-green disabled:opacity-50"
        >
          {ejectProfiles.length === 0 ? (
            <option value="">{t('printers.eject.foreignNoProfiles')}</option>
          ) : (
            ejectProfiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.id === dialog.suggestedEjectProfileId
                  ? ` (${t('printers.eject.foreignSuggested')})`
                  : ''}
              </option>
            ))
          )}
        </select>
      </div>
      {error && (
        <InlineAlert severity="error" className="mb-4 text-xs">
          {error}
        </InlineAlert>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onClose}
          disabled={isPending}
          className="flex-1 px-3 py-2 rounded-lg text-xs font-medium bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
        >
          {t('printers.eject.cancel')}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={isPending || selectedProfileId === null || !heightValid}
          className="flex-1 inline-flex items-center justify-center gap-2 px-3 py-2 rounded-lg text-xs font-medium bg-red-500/20 border border-red-400/40 text-red-300 hover:bg-red-500/30 transition-colors disabled:opacity-50"
        >
          {isPending ? (
            <Loader2 className="w-3 h-3 animate-spin" />
          ) : (
            <Wind className="w-3 h-3" />
          )}
          {t('printers.eject.foreignConfirm')}
        </button>
      </div>
    </Modal>
  );
}
