/**
 * The Fleet tab's draggable widget grid.
 *
 * Presentational over LOADED data: the tab owns the query, the loading triad
 * and the error surface, and hands this component an overview it already has.
 * That is what keeps the six widgets free of state homes — there is exactly one
 * `['fleet-metrics','overview',…]` query on the page, and it is not here.
 *
 * The grid itself is the app's existing `Dashboard` (drag to reorder, hide,
 * resize, persisted per storage key), mounted the same way the Prints tab
 * mounts it: `hideControls`, with the PAGE rendering "Reset layout" and the
 * hidden-count button so they can act on whichever tab is open. See
 * `FleetWidgetsProps` for exactly what the page has to do.
 *
 * `FleetPatternDefs` is mounted here, once. The `<pattern>` defs are
 * document-scoped paint servers, so one mount serves every chart in the grid;
 * mounting them per widget would define the same three ids six times over.
 */
import { useTranslation } from 'react-i18next';
import { Dashboard, type DashboardWidget } from '../Dashboard';
import { FleetPatternDefs } from './FleetPatternDefs';
import { CoolingAndEjectWidget } from './widgets/CoolingAndEjectWidget';
import { DowntimeByCauseWidget } from './widgets/DowntimeByCauseWidget';
import { PartsBySkuWidget } from './widgets/PartsBySkuWidget';
import { PrintsPerDayWidget } from './widgets/PrintsPerDayWidget';
import { RecoveryWidget } from './widgets/RecoveryWidget';
import { StateOverTimeWidget } from './widgets/StateOverTimeWidget';
import type { FleetOverview } from '../../types/fleetMetrics';
import { FLEET_DASHBOARD_STORAGE_KEY } from '../../utils/fleetMetrics';

/**
 * Where the grid stops being a grid.
 *
 * 1024 rather than the Prints tab's 640: these widgets carry a legend and a
 * data table under a plot, and a half-width card below a tablet is too narrow
 * for a dated axis to stay legible.
 */
export const FLEET_STACK_BELOW_PX = 1024;

export interface FleetWidgetsProps {
  /**
   * One loaded window. The tab has already resolved loading, error and empty —
   * this component renders numbers, never a spinner.
   */
  overview: FleetOverview;
}

export function FleetWidgets({ overview }: FleetWidgetsProps) {
  const { t } = useTranslation();

  const widgets: DashboardWidget[] = [
    {
      id: 'fleet-state-over-time',
      title: t('fleetMetrics.sections.stateOverTime'),
      component: (size) => <StateOverTimeWidget overview={overview} size={size} />,
      defaultSize: 4,
    },
    {
      id: 'fleet-prints-per-day',
      title: t('fleetMetrics.sections.printsPerDay'),
      component: (size) => <PrintsPerDayWidget overview={overview} size={size} />,
      defaultSize: 2,
    },
    {
      id: 'fleet-downtime-by-cause',
      title: t('fleetMetrics.sections.downtimeByCause'),
      component: (size) => <DowntimeByCauseWidget overview={overview} size={size} />,
      defaultSize: 2,
    },
    {
      id: 'fleet-cooling-and-eject',
      title: t('fleetMetrics.sections.coolingAndEject'),
      component: <CoolingAndEjectWidget overview={overview} />,
      defaultSize: 2,
    },
    {
      id: 'fleet-recovery',
      title: t('fleetMetrics.sections.recovery'),
      component: (size) => <RecoveryWidget overview={overview} size={size} />,
      defaultSize: 2,
    },
    {
      id: 'fleet-parts-by-sku',
      title: t('fleetMetrics.sections.partsBySku'),
      component: (size) => <PartsBySkuWidget overview={overview} size={size} />,
      defaultSize: 2,
    },
  ];

  return (
    <>
      <FleetPatternDefs />
      <Dashboard
        widgets={widgets}
        storageKey={FLEET_DASHBOARD_STORAGE_KEY}
        stackBelow={FLEET_STACK_BELOW_PX}
        hideControls
      />
    </>
  );
}

export default FleetWidgets;
