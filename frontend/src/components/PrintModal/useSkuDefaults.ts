import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';

/**
 * SKU-derived defaults for the file the PrintModal is printing (plan 2b).
 * `defaultEjectProfileId` is the eject profile the linking SKU nominates;
 * `skuCode` labels where it came from for the UI hint. Both null when the file
 * isn't linked to any SKU.
 */
export interface SkuDefaults {
  defaultEjectProfileId: number | null;
  skuCode: string | null;
}

/**
 * Resolve the SKU that links the given library file and surface its defaults.
 *
 * A SKU references one or more library files; when the modal prints a library
 * file a SKU owns, the SKU's `default_eject_profile_id` becomes the suggested
 * eject profile (the same derivation the production-run StartRunDialog already
 * uses). Archives carry no library-file link the modal can see, so archive
 * prints pass `null` here and resolve to no default — we never issue an extra
 * request to chase one down.
 *
 * Shares the `['skus']` query cache with SkusPage / ProductionRunsPage.
 */
export function useSkuDefaults(libraryFileId: number | null | undefined): SkuDefaults {
  const { data: skus } = useQuery({
    queryKey: ['skus'],
    queryFn: api.getSkus,
    enabled: libraryFileId != null,
  });

  if (libraryFileId == null || !skus) {
    return { defaultEjectProfileId: null, skuCode: null };
  }

  const match = skus.find((sku) =>
    sku.files.some((file) => file.library_file_id === libraryFileId),
  );
  if (!match) {
    return { defaultEjectProfileId: null, skuCode: null };
  }

  return {
    defaultEjectProfileId: match.default_eject_profile_id,
    skuCode: match.code,
  };
}
