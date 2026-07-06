import { useEffect } from 'react';
import type { PlateMetadata } from '../types/plates';

/**
 * Keep a controlled plate `<select>` in sync with an async-loaded plate list.
 *
 * Every plate picker seeds `plateIndex` to 1 when a file is chosen, but the
 * plates query resolves later and — for partially-sliced multi-plate 3MFs —
 * may not contain plate 1 at all (the backend lists only plates that carry
 * G-code). Without this snap, the select DISPLAYS the first real option while
 * the state still holds 1, and submitting sends a plate the backend rejects
 * with "Plate 1 has no G-code".
 */
export function usePlateIndexSync(
  plates: PlateMetadata[],
  plateIndex: number,
  setPlateIndex: (v: number) => void,
): void {
  useEffect(() => {
    if (plates.length > 0 && !plates.some((p) => p.index === plateIndex)) {
      setPlateIndex(plates[0].index);
    }
  }, [plates, plateIndex, setPlateIndex]);
}
