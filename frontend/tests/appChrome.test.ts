import {
  shouldShowCellBreadcrumb,
  shouldShowConverter,
  shouldPollConverterStatus,
  sourceContentMode,
} from '../src/appChrome'
import type { AppState } from '../src/types'


function sourceState(sourceName: string): AppState {
  return { view: 'source', sourceName, sourcePath: `/root/${sourceName}` }
}


function assertEqual(actual: unknown, expected: unknown) {
  if (actual !== expected) {
    throw new Error(`Expected ${String(expected)}, got ${String(actual)}`)
  }
}


assertEqual(shouldShowConverter({ view: 'library' }), false)
assertEqual(shouldShowConverter(sourceState('lerobot')), true)
assertEqual(shouldShowConverter(sourceState('lerobot_test')), false)
assertEqual(shouldShowConverter({ view: 'converter' }), true)
assertEqual(shouldPollConverterStatus(sourceState('lerobot')), false)
assertEqual(shouldPollConverterStatus({ view: 'converter' }), true)
assertEqual(sourceContentMode(2), 'cells')
assertEqual(sourceContentMode(0), 'datasets')
assertEqual(
  shouldShowCellBreadcrumb({
    view: 'dataset',
    sourceName: 'lerobot',
    sourcePath: '/root/lerobot',
    cellName: 'cell001',
    cellPath: '/root/lerobot/cell001',
    datasetPath: '/root/lerobot/cell001/dataset_a',
    datasetName: 'dataset_a',
    tab: 'overview',
  }),
  true,
)
assertEqual(
  shouldShowCellBreadcrumb({
    view: 'dataset',
    sourceName: 'lerobot_test',
    sourcePath: '/root/lerobot_test',
    cellName: 'lerobot_test',
    cellPath: '/root/lerobot_test',
    datasetPath: '/root/lerobot_test/HZ_seqpick_deodorant',
    datasetName: 'HZ_seqpick_deodorant',
    tab: 'overview',
  }),
  false,
)
