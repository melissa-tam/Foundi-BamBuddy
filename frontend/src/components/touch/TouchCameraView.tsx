/**
 * A tile's camera, full screen: the existing live viewer (`pages/CameraPage`, the one the
 * Printers page opens in its own window) inside the touch shell, with a 56 px Back to the
 * touch view floating over it.
 */
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft } from 'lucide-react';
import { CameraPage } from '../../pages/CameraPage';

export function TouchCameraView() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="relative h-full min-h-0 flex-1 overflow-hidden">
      <CameraPage />
      <button
        type="button"
        onClick={() => navigate('/touch')}
        className="fixed bottom-4 left-4 z-40 flex min-h-[56px] items-center gap-2 rounded-xl bg-bambu-dark-secondary/90 px-5 text-base font-semibold text-white shadow-lg hover:bg-bambu-dark-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
      >
        <ChevronLeft className="h-6 w-6" aria-hidden="true" />
        {t('touch.backToTouch')}
      </button>
    </div>
  );
}
