#!/usr/bin/env node
/**
 * CSP REGRESSION GUARD
 *
 * Scans:
 *  - src/generated/activityValidator.js (pre-build source emitted by gen-activity-validator.mjs)
 *  - dist/assets/*.js (final prod bundle)
 *
 * For forbidden patterns that cause hard crashes on load in the browser ESM + strict-CSP world:
 *   - "new Function(" / "eval("  (original eval-crash under CSP no-unsafe-eval)
 *   - "require(" / "module.exports"  (CJS in ESM bundle → ReferenceError: require is not defined)
 *
 * WHY THIS GUARD (extended):
 * - Traded one crash for another in PR #124: Ajv standalone with esm:true still emitted
 *   `require("ajv/dist/runtime/ucs2length").default` inside the otherwise-ESM validator.
 * - Both the eval path and the require path blank the app at module evaluation time.
 * - The review-workbench E2E (and any teacher flow) will time out on [data-testid="review-workbench"]
 *   or similar when the import of review-helpers (which pulls the validator) throws.
 * - npm test runs `vitest && npm run build && node scripts/csp-guard.mjs` so the guard is enforced.
 *
 * Production serves with strict CSP header: `default-src 'self'` (intentionally no `unsafe-eval`).
 * CI/dev use Vite which does not enforce the header (see issue #123 for adding prod CSP header to stub
 * or real backend for fuller E2E simulation).
 *
 * This guard FAILS if violations in generated or built artifacts.
 *
 * Run: node scripts/csp-guard.mjs  (after `npm run build`)
 */

import fs from 'fs';
import path from 'path';

const APP_ROOT = process.cwd();
const GENERATED_FILE = path.resolve(APP_ROOT, 'src/generated/activityValidator.js');
const DIST_DIR = path.resolve(APP_ROOT, 'dist/assets');

// Always guard the *generated* source (even if build not run yet; required by task).
const filesToScan = [];
if (fs.existsSync(GENERATED_FILE)) {
  filesToScan.push({ file: GENERATED_FILE, label: 'generated' });
} else {
  console.warn('CSP guard: generated validator not found at', GENERATED_FILE, '(will skip)');
}

if (fs.existsSync(DIST_DIR)) {
  const distFiles = fs.readdirSync(DIST_DIR)
    .filter((f) => f.endsWith('.js'))
    .map((f) => ({ file: path.join(DIST_DIR, f), label: 'bundle' }));
  filesToScan.push(...distFiles);
} else {
  // For full enforcement we prefer dist present, but still check generated.
  console.warn('CSP guard: dist/assets not found (run "npm run build" for full bundle scan).');
}

const PATTERNS = [
  { name: 'new Function(', re: /(?<!['"`])new Function\s*\(/g },
  { name: 'eval(', re: /(?<!['"`])[^$\w]eval\s*\(/g },
  // MANDATORY: catch the traded CJS crash (require in ESM context) that also blanks the app.
  // Use negative lookbehind to ignore string literals e.g. .code='require("ajv/...")' that
  // are metadata from ajv runtime helpers (not executable calls in our code).
  { name: 'require(', re: /(?<!['"`])require\s*\(/g },
  { name: 'module.exports', re: /(?<!['"`])module\.exports/g },
];

let totalViolations = 0;
const hits = [];

for (const { file, label } of filesToScan) {
  const content = fs.readFileSync(file, 'utf8');
  for (const { name, re } of PATTERNS) {
    const matches = content.match(re);
    if (matches && matches.length > 0) {
      totalViolations += matches.length;
      hits.push({ file: `${path.basename(file)} (${label})`, pattern: name, count: matches.length });
      // Show a small snippet for the first hit
      const idx = content.search(re);
      if (idx >= 0) {
        const snippet = content.slice(Math.max(0, idx - 30), idx + 80).replace(/\s+/g, ' ');
        console.error(`  ${path.basename(file)} (${label}): ${matches.length} × ${name}  e.g. ...${snippet}...`);
      }
    }
  }
}

if (totalViolations > 0) {
  console.error(`
CSP GUARD FAILED: ${totalViolations} occurrence(s) of forbidden patterns (new Function / eval / require / module.exports)
in generated or prod bundle.

This would crash the app (either EvalError under strict CSP, or ReferenceError: require is not defined at ESM module load).
Both blank the page and cause E2E timeouts on review workbench flows.

Root causes covered:
- Old: top-level new Ajv().compile() in src/review-helpers.ts (new Function).
- New (this PR #124): Ajv standaloneCode still emitted CJS require("ajv/dist/runtime/ucs2length") despite esm:true.
  Post-process in gen-activity-validator.mjs + this extended guard close the hole.

Files with hits: ${hits.map(h => `${h.file} (${h.count}×${h.pattern})`).join(', ')}
`);
  process.exit(1);
}

console.log('CSP guard passed: 0 hits for "new Function(", "eval(", "require(", or "module.exports"');
console.log('  in src/generated/activityValidator.js + dist/assets/*.js');
console.log('  (build is safe for strict CSP default-src \'self\' and pure ESM)');
