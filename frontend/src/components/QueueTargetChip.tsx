/**
 * The target a queue row dispatches against, as one inline chip.
 *
 * Assignment is an ATTRIBUTE of a row, not a lane it is sorted into: the queue
 * is one priority-ordered list where order is the only axis, so every row that
 * can carry work states its own target. Extracted from the unit row so the
 * collapsed batch (run) row can say the same thing in the same shape — a
 * collapsed run used to show no target at all, which is what made the lane
 * grouping look load-bearing.
 *
 * Copy comes from `utils/queueTarget.describeQueueTarget` and nowhere else; a
 * pool target adds the unit's own narrowing suffixes (`@ location`, the
 * required filament list), a pinned or unassigned row is the bare target.
 */
import { Printer } from 'lucide-react';
import type { PrintQueueItem } from '../api/client';
import { describeQueueTarget, queueTargetKindClasses } from '../utils/queueTarget';
import type { PrinterNameSource, QueueTargetItem } from '../utils/queueTarget';

/** The fields the chip reads — a queue item satisfies it, and so does any row
 *  shaped like one. The two extra columns are the pool suffixes. */
export type QueueTargetChipItem = QueueTargetItem &
  Pick<PrintQueueItem, 'target_location' | 'required_filament_types'>;

interface QueueTargetChipProps {
  item: QueueTargetChipItem;
  t: (key: string, options?: Record<string, unknown>) => string;
  /** Fleet names for a printers-pool label, built once by the page. */
  printerNameById: PrinterNameSource;
}

export function QueueTargetChip({ item, t, printerNameById }: QueueTargetChipProps) {
  const target = describeQueueTarget(item, t, printerNameById);
  const isPool = target.kind === 'model' || target.kind === 'printers';
  const text = isPool
    ? `${target.label}${item.target_location ? ` @ ${item.target_location}` : ''}${
        item.required_filament_types?.length ? ` (${item.required_filament_types.join(', ')})` : ''
      }`
    : target.label;

  return (
    <span className={`flex items-center gap-1 sm:gap-1.5 ${queueTargetKindClasses(target.kind).chip}`}>
      <Printer className="w-3 h-3 sm:w-3.5 sm:h-3.5" />
      {/* The label truncates on narrow widths, so the full text lives in the
          tooltip rather than being lost. */}
      <span className="truncate max-w-[120px] sm:max-w-none" title={text}>
        {text}
      </span>
    </span>
  );
}
