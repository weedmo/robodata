import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

function assertIncludes(actual: string, expected: string) {
  if (!actual.includes(expected)) {
    throw new Error(`Expected CSS to include: ${expected}`)
  }
}

const testDir = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(join(testDir, '../src/App.css'), 'utf8')

assertIncludes(css, '.cvp-card.is-live')
assertIncludes(css, '@keyframes cvp-live-pulse')
assertIncludes(css, '.cvp-card.is-failure-flash')
assertIncludes(css, '.cvp-card-bar-ghost')
assertIncludes(css, '.cvp-live-line')
assertIncludes(css, '.cvp-live-line .dot')
assertIncludes(css, '.cvp-live-line.cvp-live-finalizing')
assertIncludes(css, '.cvp-live-line.cvp-live-done')
assertIncludes(css, '.cvp-live-serial')
assertIncludes(css, '.cvp-card-bar-ghost,')
assertIncludes(css, '.cvp-live-line .dot,')
assertIncludes(css, '.cvp-card.is-live {')

console.log('converterProgressStyles: all selectors present')
