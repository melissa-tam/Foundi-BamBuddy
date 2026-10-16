/**
 * The touch view's shell (`/touch`): full screen, no sidebar, no page scroll — the
 * SpoolBuddy kiosk precedent. It mounts the app-wide prompts (`GlobalPrompts`) once, so a
 * spool prompt or a plate alert still reaches the operator standing at the touch screen.
 */
import { Outlet } from 'react-router-dom';
import { GlobalPrompts } from '../GlobalPrompts';

export function TouchLayout() {
  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden bg-bambu-dark text-white">
      <Outlet />
      <GlobalPrompts />
    </div>
  );
}
