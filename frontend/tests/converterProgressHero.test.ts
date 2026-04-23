import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

function assertIncludes(actual: string, expected: string) {
  if (!actual.includes(expected)) {
    throw new Error(`Expected source to include: ${expected}`)
  }
}

const testDir = dirname(fileURLToPath(import.meta.url))
const progressSource = readFileSync(join(testDir, '../src/components/ConverterProgress.tsx'), 'utf8')
const css = readFileSync(join(testDir, '../src/App.css'), 'utf8')

assertIncludes(progressSource, 'const lastEvent = events[events.length - 1]')
assertIncludes(progressSource, "lastEvent.type === 'scan'")
assertIncludes(progressSource, 'cvp-pill cvp-pill-scanning')
assertIncludes(progressSource, 'Scanning…')
assertIncludes(progressSource, 'role="status" aria-live="polite"')

assertIncludes(css, '.cvp-pill-scanning')
assertIncludes(css, '.cvp-pill-scanning { animation: none; }')

console.log('converterProgressHero: scanning pill wiring present')
