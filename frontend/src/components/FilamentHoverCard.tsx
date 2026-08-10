import { useState, useRef, useEffect, useLayoutEffect, useId, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Droplets, Copy, Check, Settings2, Package, PackagePlus, Unlink, RefreshCw } from 'lucide-react';
import { isLightColor } from '../utils/colors';
import type { EmptySlotKind } from '../utils/amsHelpers';
import { resolveSpoolBindingStatus, type SlotPresence } from '../utils/spoolBindingStatus';
import { Modal } from './ui/Modal';
import { ConfirmModal } from './ConfirmModal';

interface FilamentData {
  vendor: 'Bambu Lab' | 'Generic';
  profile: string;
  colorName: string;
  colorHex: string | null;
  kFactor: string;
  fillLevel: number | null; // null = unknown
  trayUuid?: string | null; // Bambu Lab spool UUID for Spoolman linking
  tagUid?: string | null; // Generic NFC tag UID fallback for linking
  fillSource?: 'ams' | 'spoolman' | 'inventory'; // Source of fill level data
}

interface SpoolmanConfig {
  enabled: boolean;
  onLinkSpool?: () => void;
  onUnlinkSpool?: () => void;
  linkedSpoolId?: number | null; // Spoolman spool ID if this tray is already linked
  spoolmanUrl?: string | null; // Base URL for Spoolman (for "Open in Spoolman" link)
  syncMode?: string | null; // If auto-sync is enabled, we may want to hide the unlink option for Bambu spools
}

interface InventoryConfig {
  onAssignSpool?: () => void;
  onUnassignSpool?: () => void;
  assignedSpool?: { id: number; material: string; brand: string | null; color_name: string | null; remainingWeightGrams?: number | null } | null;
  isAssigned?: boolean;
  // Reused-tag re-spool: set only when the tray carries a valid RFID identity
  // (tag_uid / tray_uuid). Opens the manual re-spool modal for the slot.
  onRespoolTag?: () => void;
  // Tagless "New roll…" (W5a): set ONLY when the slot's bound spool carries no
  // tag_uid. Retires the current ledger row and mints a fresh full one, which is
  // the operator's only signal that an untagged roll was physically swapped —
  // a tagless slot has no RFID event to infer it from.
  onNewRoll?: () => void;
}

interface ConfigureSlotConfig {
  enabled: boolean;
  onConfigure?: () => void;
}

interface FilamentHoverCardProps {
  data: FilamentData;
  children: ReactNode;
  disabled?: boolean;
  className?: string;
  spoolman?: SpoolmanConfig;
  inventory?: InventoryConfig;
  configureSlot?: ConfigureSlotConfig;
  actions?: ReactNode;
}

/**
 * A hover card that displays filament details when hovering over AMS slots.
 * Replaces the basic browser tooltip with a styled popover.
 */
export function FilamentHoverCard({ data, children, disabled, className = '', spoolman, inventory, configureSlot, actions }: FilamentHoverCardProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState<'top' | 'bottom'>('top');
  // Screen-space coordinates for the portaled card (#1336 follow-up). Using
  // a portal + position:fixed lets the popover escape sibling printer cards
  // that create their own stacking contexts on the dashboard — without this,
  // a card later in DOM order draws over the hover popover regardless of
  // z-index because z-index doesn't cross stacking-context boundaries.
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
  const [copied, setCopied] = useState(false);
  const [showUnlinkConfirm, setShowUnlinkConfirm] = useState(false);
  const unlinkTitleId = useId();
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleCopyUuid = () => {
    const uuid = data.trayUuid;
    if (!uuid) return;

    // Try modern clipboard API first, fallback to execCommand
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(uuid).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      }).catch(() => {
        // Fallback on error
        fallbackCopy(uuid);
      });
    } else {
      fallbackCopy(uuid);
    }
  };

  const fallbackCopy = (text: string) => {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand('copy');
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      console.error('Failed to copy to clipboard');
    }
    document.body.removeChild(textarea);
  };

  // Compute placement (top/bottom) + screen coordinates for the portaled
  // card. Runs on visibility change, scroll, and resize so the popover
  // tracks the trigger when the viewport moves. useLayoutEffect rather
  // than useEffect so the first paint already has the correct coords —
  // avoids a one-frame flicker at (0, 0).
  useLayoutEffect(() => {
    if (!isVisible) {
      setCoords(null);
      return;
    }
    const compute = () => {
      if (!triggerRef.current || !cardRef.current) return;
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const cardWidth = cardRef.current.offsetWidth;
      const headerHeight = 56;
      const spaceAbove = triggerRect.top - headerHeight;
      const spaceBelow = window.innerHeight - triggerRect.bottom;
      const placement: 'top' | 'bottom' =
        spaceAbove < cardHeight + 12 && spaceBelow > spaceAbove ? 'bottom' : 'top';
      const centerX = triggerRect.left + triggerRect.width / 2;
      const left = Math.max(8, Math.min(centerX - cardWidth / 2, window.innerWidth - cardWidth - 8));
      const top = placement === 'top' ? triggerRect.top - cardHeight - 8 : triggerRect.bottom + 8;
      setPosition(placement);
      setCoords({ top, left });
    };
    // First compute is synchronous from the layout effect; a follow-up rAF
    // re-measures after the card actually has its rendered dimensions.
    compute();
    const rafId = requestAnimationFrame(compute);
    window.addEventListener('scroll', compute, true);
    window.addEventListener('resize', compute);
    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener('scroll', compute, true);
      window.removeEventListener('resize', compute);
    };
  }, [isVisible]);

  const handleMouseEnter = () => {
    if (disabled) return;
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    // Small delay to prevent flicker on quick mouse movements
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  // Get fill bar color based on percentage
  const getFillColor = (fill: number): string => {
    if (fill <= 15) return '#ef4444'; // red
    if (fill <= 30) return '#f97316'; // orange
    if (fill <= 50) return '#eab308'; // yellow
    return '#22c55e'; // green
  };

  const colorHex = data.colorHex ? `#${data.colorHex.replace('#', '')}` : null;
  const assignedRemainingWeight = inventory?.assignedSpool?.remainingWeightGrams ?? null;

  return (
    <div
      ref={triggerRef}
      data-testid="filament-slot"
      className={`relative ${className}`}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {/* Portaled hover card — rendered into document.body so it escapes
          any ancestor stacking context. Sibling printer cards on the
          dashboard create their own stacking contexts; without the portal
          the popover gets covered by the next card even at z-[60]
          (#1336 follow-up). */}
      {isVisible && createPortal(
        <div
          ref={cardRef}
          className="fixed z-[60]"
          style={{
            top: coords?.top ?? -9999,
            left: coords?.left ?? -9999,
            maxWidth: 'calc(100vw - 24px)',
            // Hide until coords are computed to avoid a (-9999,-9999) flash.
            visibility: coords ? 'visible' : 'hidden',
          }}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          {/* Card container */}
          <div className="
            w-52 bg-bambu-dark-secondary border border-bambu-dark-tertiary
            rounded-lg shadow-xl overflow-hidden
            backdrop-blur-sm
          ">
            {/* Color swatch header - the hero element */}
            <div
              className="h-12 relative overflow-hidden"
              style={{
                backgroundColor: colorHex || '#3d3d3d',
              }}
            >
              {/* Subtle gradient overlay for depth */}
              <div className="absolute inset-0 bg-gradient-to-b from-white/10 to-transparent" />

              {/* Color name on swatch */}
              <div className={`
                absolute inset-0 flex items-center justify-center
                font-semibold text-sm tracking-wide
                ${isLightColor(colorHex) ? 'text-black/80' : 'text-white/90'}
              `}>
                {data.colorName}
              </div>

              {/* Vendor badge - solid background for visibility on any color */}
              <div className={`
                absolute top-1.5 right-1.5 px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider
                ${data.vendor === 'Bambu Lab'
                  ? 'bg-black/60 text-white'
                  : 'bg-black/50 text-white/90'}
              `}>
                {data.vendor === 'Bambu Lab' ? 'BBL' : 'GEN'}
              </div>
            </div>

            {/* Details section */}
            <div className="p-3 space-y-2.5">
              {/* Profile name */}
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                  {t('ams.profile')}
                </span>
                <span className="text-xs text-white font-semibold truncate max-w-[120px]">
                  {data.profile}
                </span>
              </div>

              {/* K Factor */}
              <div className="flex items-center justify-between">
                <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                  {t('ams.kFactor')}
                </span>
                <span className="text-xs text-bambu-green font-mono font-bold">
                  {data.kFactor}
                </span>
              </div>

              {/* Fill Level */}
              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium flex items-center gap-1">
                    <Droplets className="w-3 h-3" />
                    {t('ams.fill')}
                  </span>
                  <span className="text-xs text-white font-semibold flex items-center gap-1">
                    <span>{data.fillLevel !== null ? `${data.fillLevel}%` : '—'}</span>
                    {assignedRemainingWeight !== null && data.fillLevel !== null && (
                      <span className="text-[9px] text-bambu-gray font-normal">• {assignedRemainingWeight}g</span>
                    )}
                  </span>
                </div>
                {/* Fill bar */}
                <div className="h-1.5 bg-black/40 rounded-full overflow-hidden">
                  {data.fillLevel !== null ? (
                    <div
                      className="h-full rounded-full transition-all duration-300"
                      style={{
                        width: `${data.fillLevel}%`,
                        backgroundColor: getFillColor(data.fillLevel),
                      }}
                    />
                  ) : (
                    <div className="h-full w-full bg-bambu-gray/30 rounded-full" />
                  )}
                </div>
              </div>

              {/* Spoolman section - only show if enabled */}
              {spoolman?.enabled && (
                <div className="pt-2 mt-2 border-t border-bambu-dark-tertiary space-y-2">
                  {/* Tray UUID with copy button */}
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                      {t('spoolman.spoolId')}
                    </span>
                    {data.trayUuid ? (
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleCopyUuid();
                        }}
                        className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white transition-colors"
                        title="Copy spool UUID"
                      >
                        <span className="font-mono text-[10px] truncate max-w-[80px]">
                          {data.trayUuid.slice(0, 8)}...
                        </span>
                        {copied ? (
                          <Check className="w-3 h-3 text-bambu-green" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                      </button>
                    ) : (
                      <span className="text-[10px] text-bambu-gray">—</span>
                    )}
                  </div>

                  {/* Open in inventory button (when already linked to a Spoolman spool) */}
                  {spoolman.linkedSpoolId && (
                    <>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/inventory?spool=${spoolman.linkedSpoolId}`);
                        }}
                        className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-green/20 hover:bg-bambu-green/30 text-bambu-green"
                        title={t('inventory.openInInventory')}
                      >
                        <Package className="w-3.5 h-3.5" />
                        {t('inventory.openInInventory')}
                      </button>

                    </>
                  )}

                  {/* Link/Unlink action buttons intentionally NOT rendered
                      here. The inventory section below already provides
                      Assign/Unassign for slot-binding (the primary user
                      flow in Spoolman mode). Showing the spoolman tag-link
                      buttons in addition surfaced two red Unlink-icon
                      buttons for what users perceive as the same action,
                      regardless of whether the labels said "Unlink Spool"
                      vs "Unassign Spool". Tag-linking remains available
                      via dedicated UI (LinkSpoolModal can be opened from
                      Spoolman settings / inventory page). */}
                </div>
              )}

              {/* Inventory section — shown for every vendor including
                  Bambu Lab (#1133). The earlier "non-Bambu only" gate
                  prevented users from manually assigning a Bambu spool
                  in inventory to an AMS slot when they didn't want to
                  re-scan via SpoolBuddy NFC. */}
              {inventory && (
                <div className="pt-2 mt-2 border-t border-bambu-dark-tertiary space-y-2">
                  {inventory.assignedSpool ? (
                    <>
                      <div className="flex items-center gap-1.5">
                        <Package className="w-3 h-3 text-bambu-green" />
                        <span className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                          {t('inventory.assigned')}
                        </span>
                      </div>
                      <div className="flex items-baseline gap-1.5 min-w-0 mb-1">
                        <p className="text-xs text-white truncate">
                          {inventory.assignedSpool.brand ? `${inventory.assignedSpool.brand} ` : ''}
                          {inventory.assignedSpool.material}
                          {inventory.assignedSpool.color_name ? ` - ${inventory.assignedSpool.color_name}` : ''}
                        </p>
                        <span className="text-[10px] font-mono text-bambu-gray shrink-0">#{inventory.assignedSpool.id}</span>
                      </div>
                      {(!spoolman?.linkedSpoolId || inventory.assignedSpool!.id !== spoolman.linkedSpoolId) && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            navigate(`/inventory?spool=${inventory.assignedSpool!.id}`);
                          }}
                          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-green/20 hover:bg-bambu-green/30 text-bambu-green"
                          title={t('inventory.openInInventory')}
                        >
                          <Package className="w-3.5 h-3.5" />
                          {t('inventory.openInInventory')}
                        </button>
                      )}
                      {inventory.onUnassignSpool && (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            inventory.onUnassignSpool?.();
                          }}
                          className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-red-500/20 hover:bg-red-500/30 text-red-400"
                        >
                          <Unlink className="w-3.5 h-3.5" />
                          {t('inventory.unassignSpool')}
                        </button>
                      )}
                    </>
                  ) : inventory.onAssignSpool ? (
                    <button
                      onClick={inventory.isAssigned ? undefined : (e) => {
                        e.stopPropagation();
                        inventory.onAssignSpool?.();
                      }}
                      disabled={!!inventory.isAssigned}
                      className={`w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 text-bambu-blue ${
                        inventory.isAssigned ? 'opacity-50 cursor-not-allowed' : 'hover:bg-bambu-blue/30'
                      }`}
                    >
                      <Package className="w-3.5 h-3.5" />
                      {t('inventory.assignSpool')}
                    </button>
                  ) : null}
                  {/* Re-spool a reused Bambu tag now on a fresh third-party
                      spool — shown only when the tray has a valid RFID
                      identity (parent gates via isBambuLabSpool). */}
                  {inventory.onRespoolTag && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        inventory.onRespoolTag?.();
                      }}
                      aria-label={t('inventory.respool.action')}
                      className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/30 text-bambu-blue"
                    >
                      <RefreshCw className="w-3.5 h-3.5" />
                      {t('inventory.respool.action')}
                    </button>
                  )}
                  {/* Tagless counterpart of "Re-spool tag…": the untagged roll on
                      this slot was physically replaced. */}
                  {inventory.onNewRoll && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        inventory.onNewRoll?.();
                      }}
                      aria-label={t('inventory.freshRoll.manualAction')}
                      className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/30 text-bambu-blue"
                    >
                      <PackagePlus className="w-3.5 h-3.5" />
                      {t('inventory.freshRoll.manualAction')}
                    </button>
                  )}
                </div>
              )}

              {/* Configure slot section - always show if enabled */}
              {configureSlot?.enabled && (
                <div className={`${spoolman?.enabled && data.trayUuid ? '' : 'pt-2 mt-2 border-t border-bambu-dark-tertiary'}`}>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      configureSlot.onConfigure?.();
                    }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/30 text-bambu-blue"
                    title={t('ams.configureSlot')}
                  >
                    <Settings2 className="w-3.5 h-3.5" />
                    {t('ams.configure')}
                  </button>
                </div>
              )}
              {actions && (
                <div className="pt-2 mt-2 border-t border-bambu-dark-tertiary space-y-1">
                  {actions}
                </div>
              )}
            </div>
          </div>

          {/* Arrow pointer */}
          <div
            className={`
              absolute left-1/2 -translate-x-1/2 w-0 h-0
              border-l-[6px] border-l-transparent
              border-r-[6px] border-r-transparent
              ${position === 'top'
                ? 'top-full border-t-[6px] border-t-bambu-dark-tertiary'
                : 'bottom-full border-b-[6px] border-b-bambu-dark-tertiary'}
            `}
          />
        </div>,
        document.body,
      )}

      {/* Unlink Confirmation Dialog */}
      {showUnlinkConfirm && (
        <Modal
          onClose={() => setShowUnlinkConfirm(false)}
          overlayZIndex="z-[100]"
          widthClass="max-w-sm"
          labelledBy={unlinkTitleId}
        >
          <div className="p-4 space-y-4">
            <div className="space-y-2">
              <h3 id={unlinkTitleId} className="text-base font-semibold text-white">
                {t('spoolman.unlinkConfirmTitle')}
              </h3>
              <p className="text-sm text-bambu-gray">
                {t('spoolman.unlinkConfirmMessage')}
              </p>
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => setShowUnlinkConfirm(false)}
                className="flex-1 px-3 py-2 text-sm font-medium rounded transition-colors bg-bambu-dark hover:bg-bambu-dark-tertiary text-white"
              >
                {t('common.cancel')}
              </button>
              <button
                onClick={() => {
                  spoolman?.onUnlinkSpool?.();
                  setShowUnlinkConfirm(false);
                }}
                className="flex-1 px-3 py-2 text-sm font-medium rounded transition-colors bg-red-500/20 hover:bg-red-500/30 text-red-400"
              >
                {t('inventory.unassignSpool')}
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}

/**
 * A spool binding that survives on a slot the printer reports EMPTY (W5a).
 *
 * Three shapes reach this state and the operator must be able to tell them
 * apart: the runout latch (`spool.spent_at` — the roll ran dry and the binding
 * is deliberately held until a qualified swap), the deliberate bind-to-empty
 * (`assignment.pre_configured_at` — SpoolBuddy weigh-then-assign, the roll has
 * not been inserted yet), and everything else (a stale claim nobody cleared).
 * Callers map the API rows onto these flags; the card owns the wording.
 */
export interface EmptySlotBinding {
  spoolId: number;
  /** Brand + material + colour, already composed by the caller. */
  label: string;
  usedGrams: number;
  /** `spool.spent_at` is set — ran out, awaiting a new roll. */
  spent: boolean;
  /** `assignment.pre_configured_at` is set — awaiting the physical insert. */
  preConfigured: boolean;
  /** Live presence of the claimed slot, from the wire kind and/or the API
   *  tri-state (`slotPresence`). Without it the card used to print "not
   *  inserted" under a header saying a spool was present — the contradiction
   *  the operator reported. Optional so pre-existing fixtures still compile;
   *  absent reads as `unknown`, never as empty. */
  presence?: SlotPresence;
}

interface EmptySlotHoverCardProps {
  children: ReactNode;
  className?: string;
  configureSlot?: ConfigureSlotConfig;
  onAssignSpool?: () => void;
  actions?: ReactNode;
  // #1322 follow-up: distinguish a wire-asserted empty slot from one whose
  // presence the wire never stated. "unknown" surfaces the state-unknown label;
  // "present" (state 10/11 with no material identity — a seated spool the AMS
  // could not read) surfaces the unread-spool label; undefined / "physical"
  // keeps the historical "Empty slot" wording.
  kind?: EmptySlotKind;
  // W5a: a binding that outlived the filament. Without this the empty-slot card
  // showed NO assignment information at all, so a lingering binding was both
  // invisible and unclearable from the printer card.
  binding?: EmptySlotBinding | null;
  // Releases `binding` from the slot (DELETE /inventory/assignments/…). Gated
  // behind a confirm dialog; omit to hide the verb (e.g. no permission).
  onClearSlot?: () => void;
  /** Keeps the confirm dialog in its busy state while the DELETE is in flight. */
  clearPending?: boolean;
}

export function EmptySlotHoverCard({ children, className = '', configureSlot, onAssignSpool, actions, kind, binding, onClearSlot, clearPending }: EmptySlotHoverCardProps) {
  const { t } = useTranslation();
  const [isVisible, setIsVisible] = useState(false);
  const [showClearConfirm, setShowClearConfirm] = useState(false);
  // Screen-space coords for the portaled card — same pattern as
  // FilamentHoverCard, see comment there (#1336 follow-up).
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleMouseEnter = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(true), 80);
  };

  const handleMouseLeave = () => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    timeoutRef.current = setTimeout(() => setIsVisible(false), 100);
  };

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  useLayoutEffect(() => {
    if (!isVisible) {
      setCoords(null);
      return;
    }
    const compute = () => {
      if (!triggerRef.current || !cardRef.current) return;
      const triggerRect = triggerRef.current.getBoundingClientRect();
      const cardHeight = cardRef.current.offsetHeight;
      const cardWidth = cardRef.current.offsetWidth;
      const centerX = triggerRect.left + triggerRect.width / 2;
      const left = Math.max(8, Math.min(centerX - cardWidth / 2, window.innerWidth - cardWidth - 8));
      const top = triggerRect.top - cardHeight - 8;
      setCoords({ top, left });
    };
    compute();
    const rafId = requestAnimationFrame(compute);
    window.addEventListener('scroll', compute, true);
    window.addEventListener('resize', compute);
    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener('scroll', compute, true);
      window.removeEventListener('resize', compute);
    };
  }, [isVisible]);

  // Status wording is never carried by colour alone: each state pairs an icon
  // with an explicit sentence (WCAG 1.4.1). Precedence (spent > pre-configured)
  // and wording live in `resolveSpoolBindingStatus` — shared with the Inventory
  // LOCATION column so the two surfaces can never drift apart.
  const bindingStatus = binding ? resolveSpoolBindingStatus(binding) : null;

  // "Clear slot" is the manual escape hatch for a STALE LOCATION CLAIM, and it is
  // offered only where the claim can be stale: a wire-asserted empty slot, or one
  // whose presence the printer never stated. On a SEATED-but-unread slot it is
  // semantically void — the pipeline re-derives a binding for the roll that is
  // physically in there, so clearing removes a row that comes straight back, and
  // treating clear-slot as the resolution for an unread tray is the defect the
  // operator ruled against (it also mints the phantom rows the WS-G repair
  // archives). The honest resolution there is identification, not deletion.
  const canClearSlot = !!binding && !!onClearSlot && kind !== 'present';

  return (
    <div
      ref={triggerRef}
      className={`relative ${className}`}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {children}

      {isVisible && createPortal(
        <div
          ref={cardRef}
          className="fixed z-[60]"
          style={{
            top: coords?.top ?? -9999,
            left: coords?.left ?? -9999,
            visibility: coords ? 'visible' : 'hidden',
          }}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
        >
          <div className="
            bg-bambu-dark-secondary border border-bambu-dark-tertiary
            rounded-md shadow-lg overflow-hidden
          ">
            <div className="px-3 py-1.5 text-xs text-bambu-gray whitespace-nowrap">
              {kind === 'unknown'
                ? t('ams.slotStateUnknown')
                : kind === 'present'
                  ? t('ams.slotPresentUnread')
                  : t('ams.emptySlot')}
            </div>
            {/* Lingering binding on a physically empty slot (W5a). */}
            {binding && bindingStatus && (
              <div className="px-3 pb-2 pt-1 w-52 border-t border-bambu-dark-tertiary space-y-1">
                <p className="text-[10px] uppercase tracking-wider text-bambu-gray font-medium">
                  {t('ams.emptySlotBinding.title')}
                </p>
                <div className="flex items-baseline gap-1.5 min-w-0">
                  <p className="text-xs text-white truncate">{binding.label}</p>
                  <span className="text-[10px] font-mono text-bambu-gray shrink-0">#{binding.spoolId}</span>
                </div>
                <p className="text-[10px] text-bambu-gray">
                  {t('ams.emptySlotBinding.used', { grams: binding.usedGrams })}
                </p>
                <p className={`text-[10px] flex items-start gap-1 ${bindingStatus.className}`}>
                  <bindingStatus.Icon className="w-3 h-3 mt-px shrink-0" aria-hidden="true" />
                  <span>{t(bindingStatus.i18nKey)}</span>
                </p>
              </div>
            )}
            {/* Configure slot button */}
            {(configureSlot?.enabled || onAssignSpool || actions || canClearSlot) && (
              <div className="px-2 pb-2 space-y-1">
                {canClearSlot && binding && (
                  <button
                    onClick={(e) => { e.stopPropagation(); setShowClearConfirm(true); }}
                    aria-label={t('ams.emptySlotBinding.clearAria', { spool: binding.label })}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-amber-500/20 hover:bg-amber-500/30 text-amber-400"
                  >
                    <Unlink className="w-3.5 h-3.5" />
                    {t('ams.emptySlotBinding.clear')}
                  </button>
                )}
                {configureSlot?.enabled && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      configureSlot.onConfigure?.();
                    }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/30 text-bambu-blue"
                    title={t('ams.configureSlot')}
                  >
                    <Settings2 className="w-3.5 h-3.5" />
                    {t('ams.configure')}
                  </button>
                )}
                {onAssignSpool && (
                  <button
                    onClick={(e) => { e.stopPropagation(); onAssignSpool(); }}
                    className="w-full flex items-center justify-center gap-1.5 px-2 py-1.5 text-xs font-medium rounded transition-colors bg-bambu-blue/20 hover:bg-bambu-blue/30 text-bambu-blue"
                  >
                    <Package className="w-3.5 h-3.5" />
                    {t('inventory.assignSpool')}
                  </button>
                )}
                {actions && (
                  <div className="pt-1 mt-1 border-t border-bambu-dark-tertiary space-y-1">
                    {actions}
                  </div>
                )}
              </div>
            )}
          </div>
          <div className="
            absolute left-1/2 -translate-x-1/2 top-full w-0 h-0
            border-l-[5px] border-l-transparent
            border-r-[5px] border-r-transparent
            border-t-[5px] border-t-bambu-dark-tertiary
          " />
        </div>,
        document.body,
      )}

      {/* Clear-slot confirmation. Rendered from the always-mounted trigger (not
          the hover popover, which unmounts on mouse-out) so the dialog survives
          the pointer leaving the slot. */}
      {showClearConfirm && binding && (
        <ConfirmModal
          title={t('ams.emptySlotBinding.clearTitle')}
          message={t('ams.emptySlotBinding.clearMessage', { spool: binding.label })}
          confirmText={t('ams.emptySlotBinding.clear')}
          cancelText={t('common.cancel')}
          variant="warning"
          overlayZIndex="z-[100]"
          isLoading={!!clearPending}
          onConfirm={() => {
            onClearSlot?.();
            setShowClearConfirm(false);
          }}
          onCancel={() => setShowClearConfirm(false)}
        />
      )}
    </div>
  );
}
