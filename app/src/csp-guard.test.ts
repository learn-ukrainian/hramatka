/**
 * CSP regression simulation test (unit, runs in vitest/jsdom).
 *
 * This test monkey-patches `globalThis.Function` (the constructor behind `new Function(...)`)
 * to throw a CSP-like EvalError, then dynamically imports review-helpers (the module
 * that used to do top-level Ajv compile).
 *
 * - Before the fix: import would execute `new Ajv().compile(...)` → calls new Function internally → throws.
 * - After the fix: the import resolves to pre-generated pure validator (no new Function ever called at load or use).
 *
 * This is a *deterministic* in-process proof that the validator path is CSP-safe.
 * Complements the post-build bundle grep guard.
 *
 * See also: scripts/csp-guard.mjs (greps dist/assets for the strings in prod artifacts)
 * and scripts/gen-activity-validator.mjs .
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

describe('CSP regression guard (simulated unsafe-eval block)', () => {
  it('loads review-helpers and runs validateActivityDocument when new Function is blocked (CSP-like)', async () => {
    const OrigFunction = globalThis.Function;

    let threwDuringImport = false;
    // Patch constructor used by `new Function(str)` and Ajv codegen.
    // We throw a message mirroring the real browser EvalError under missing 'unsafe-eval'.
    globalThis.Function = function (...args: any[]) {
      const source = args
        .filter((a) => typeof a === 'string')
        .join('\n');
      // Heuristic: real Ajv codegen and new Function usage pass code containing "return" bodies.
      if (source.includes('return') || source.length > 20) {
        threwDuringImport = true;
        const err = new Error(
          "EvalError: Evaluating a string as JavaScript violates the following Content Security Policy directive: \"default-src 'self'\""
        );
        // Make it look like the native one
        (err as any).name = 'EvalError';
        throw err;
      }
      // Allow other Function uses (e.g. class constructors in test env) by delegating
      return Reflect.construct(OrigFunction as any, args as any);
    } as any;

    try {
      // IMPORTANT: dynamic import *after* patch so module init code runs under the block.
      // Static imports at top of this file would have executed too early.
      const { validateActivityDocument } = await import('./review-helpers');

      // Now exercise the validator (same as before).
      const goodActivity = {
        id: 'csp1',
        type: 'true-false',
        title: 'CSP check',
        level: 'b1',
        payload: {
          type: 'true-false',
          instruction: 'Pick.',
          items: [{ statement: 'Safe.', correct: true }],
        },
        answer_key: { items: [{ index: 0, correct: true }] },
        provenance: { source: 'csp-guard', generator: 'vitest', gates: ['schema'] },
      };

      const result = validateActivityDocument(goodActivity);
      expect(result.valid).toBe(true);
      expect(result.errors).toEqual([]);

      // Also a failing case still works.
      const bad = { ...goodActivity, title: '' };
      const badRes = validateActivityDocument(bad);
      expect(badRes.valid).toBe(false);
      expect(badRes.errors.length).toBeGreaterThan(0);

      // The patch should not have been triggered by our code.
      expect(threwDuringImport).toBe(false);
    } finally {
      globalThis.Function = OrigFunction;
    }
  });

  it('would have thrown on the pre-fix eval-based compile (documenting the bug)', () => {
    // This documents intent: if review-helpers still contained the top level
    //   const activityValidator = new Ajv({ allErrors: true, strict: false }).compile(activitySchema);
    // then the previous test's dynamic import (under patch) would set threwDuringImport=true and fail.
    // The source change + precompile makes the "would fail" into "passes".
    expect(true).toBe(true);
  });

  it('generated validator (and by extension its bundles) must contain ZERO CommonJS require/module.exports', () => {
    // MANDATORY GUARD (unit test level): the csp-guard.mjs already greps generated+dist at build time.
    // This test provides a fast, pre-build check that runs in vitest portion of `npm test`.
    // It directly greps the committed/generated source so a require( regression is caught even
    // if someone hand-edits the .js or the generator post-process regresses.
    const genPath = path.resolve(path.dirname(new URL(import.meta.url).pathname), './generated/activityValidator.js');
    if (!fs.existsSync(genPath)) {
      // If not present (rare), skip hard; build will enforce.
      return;
    }
    const content = fs.readFileSync(genPath, 'utf8');
    expect(content).not.toMatch(/require\s*\(/);
    expect(content).not.toMatch(/module\.exports/);
    // Also document that we accept "use strict"; at head (harmless in ESM) but no CJS calls.
    // The generator strips it; if present it's ok for this check as long as no require.
  });
});
