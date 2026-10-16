/**
 * The app-wide prompts every full-screen shell mounts once — the main `Layout` and the
 * touch view's `TouchLayout` — so a prompt raised while an operator stands at the touch
 * screen is not lost:
 *
 * - the per-slot spool prompts (unknown tag, re-spool, tagless fresh roll) and the
 *   durable-prompt replay that feeds them;
 * - the two new-roll dialogs those queues open;
 * - the plate-detection alert (`plate-not-empty`: objects on the plate, print paused),
 *   shown to users who can control printers.
 *
 * ONE owner: these hooks and the `plate-not-empty` listener appear nowhere else
 * (`__tests__/components/GlobalPromptsOwnership.test.ts`).
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { usePendingPromptSync } from '../hooks/usePendingPromptSync';
import { useRespoolPrompt } from '../hooks/useRespoolPrompt';
import { useTaglessFreshPrompt } from '../hooks/useTaglessFreshPrompt';
import { useUnknownTagPrompt } from '../hooks/useUnknownTagPrompt';
import { NewRollModal } from './NewRollModal';
import { Modal } from './ui/Modal';

interface PlateDetectionAlert {
  printer_id: number;
  printer_name: string;
  message: string;
}

function isPlateDetectionAlert(value: unknown): value is PlateDetectionAlert {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.printer_id === 'number' && typeof v.printer_name === 'string';
}

export function GlobalPrompts() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const [plateDetectionAlert, setPlateDetectionAlert] = useState<PlateDetectionAlert | null>(null);

  // Unknown-spool prompt — raises a per-slot persistent toast when the AMS reports a
  // tag with no inventory match (only when `auto_add_unknown_rfid` is off).
  useUnknownTagPrompt();
  const respoolPrompt = useRespoolPrompt();
  // Tagless fresh-roll prompt — a per-slot persistent toast when a non-RFID roll is
  // consumed past half its label weight; its review dialog is rendered below.
  const taglessFreshPrompt = useTaglessFreshPrompt();
  // Prompt recovery lane — replays every still-unanswered per-slot prompt on load and
  // on each websocket reconnect. Must sit AFTER the prompt hooks: it dispatches the
  // very window events they listen for.
  usePendingPromptSync();

  // Plate detection warnings (objects on plate, print paused), for users who can
  // control printers only.
  useEffect(() => {
    const handlePlateNotEmpty = (event: Event) => {
      if (!hasPermission('printers:control')) return;
      const detail: unknown = (event as CustomEvent<unknown>).detail;
      if (!isPlateDetectionAlert(detail)) return;
      setPlateDetectionAlert({
        printer_id: detail.printer_id,
        printer_name: detail.printer_name,
        message: typeof detail.message === 'string' ? detail.message : '',
      });
    };
    window.addEventListener('plate-not-empty', handlePlateNotEmpty);
    return () => window.removeEventListener('plate-not-empty', handlePlateNotEmpty);
  }, [hasPermission]);

  return (
    <>
      {/* One form, two prompt queues: each hook normalises its own WS payload into the
          shared `NewRollContext`, so the dialog never learns which question raised it. */}
      <NewRollModal context={respoolPrompt.activeContext} onClose={respoolPrompt.closeModal} />
      <NewRollModal context={taglessFreshPrompt.activeContext} onClose={taglessFreshPrompt.closeModal} />

      {plateDetectionAlert && (
        <Modal
          onClose={() => setPlateDetectionAlert(null)}
          labelledBy="plate-detection-alert-title"
          size="sm"
          closeOnOverlay={false}
          overlayZIndex="z-[100]"
          className="animate-in fade-in zoom-in duration-200"
        >
          <div className="p-6 text-center">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-yellow-500/20 flex items-center justify-center">
              <svg className="w-10 h-10 text-yellow-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <h2 id="plate-detection-alert-title" className="text-xl font-bold text-yellow-400 mb-2">
              {t('plateAlert.title')}
            </h2>
            <p className="text-lg text-white mb-2">{plateDetectionAlert.printer_name}</p>
            <p className="text-bambu-gray mb-6">{t('plateAlert.message')}</p>
            <button
              onClick={() => setPlateDetectionAlert(null)}
              className="w-full min-h-[56px] py-3 px-6 bg-yellow-500 hover:bg-yellow-600 text-black font-semibold rounded-lg transition-colors"
            >
              {t('plateAlert.understand')}
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
