#!/usr/bin/env node
/**
 * Prebuild script: generate CSP-safe Ajv standalone validator at build time.
 *
 * - Loads the vendored lu.activity.v1 schema (source of truth for the pilot contract).
 * - Uses Ajv with `code: { source: true, esm: true }` + standaloneCode to emit a pure
 *   ES module containing only static validation functions (NO `new Function`, NO runtime eval).
 * - Writes src/generated/activityValidator.js which is a drop-in replacement for the
 *   runtime-compiled validator (same call/ .errors shape).
 *
 * Post-processing (MANDATORY):
 *   Even with esm:true, Ajv standalone injects `const funcN = require("ajv/dist/runtime/ucs2length").default;`
 *   (a CJS require for a UCS-2 length helper used by minLength etc). This is fatal in ESM browser
 *   bundles: ReferenceError: require is not defined at module eval time → blank app.
 *   We post-process to:
 *     1. Strip leading "use strict"; (CJS residue).
 *     2. Prepend a proper ESM import.
 *     3. Replace the require expr with the imported binding.
 *   Result: ZERO require( / module.exports in the generated source (enforced by csp-guard).
 *
 * Run via `npm run build` (wired as prebuild). The generated file is also committed so that
 * `npm test`, typecheck, and dev server work without requiring the pre-step in every env.
 * Re-running the build always reproduces the file byte-identically (deterministic for same Ajv+schema).
 */

import Ajv from 'ajv';
import standaloneCode from 'ajv/dist/standalone/index.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// From hramatka/app/scripts/ -> hramatka/vendor/...
const SCHEMA_PATH = path.resolve(__dirname, '../../vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.schema.json');
const OUT_DIR = path.resolve(__dirname, '../src/generated');
const OUT_FILE = path.join(OUT_DIR, 'activityValidator.js');

console.log('[gen-activity-validator] Loading schema from', SCHEMA_PATH);
const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));

const ajv = new Ajv({
  allErrors: true,
  strict: false,
  code: { source: true, esm: true },
});

const validate = ajv.compile(schema);
let code = standaloneCode(ajv, validate);

// Post-process to eliminate CommonJS residues that leak from Ajv standalone even with esm:true.
// This is the deterministic root fix for "require is not defined" in the ESM browser bundle.
// We inline the tiny ucs2length helper (unicode scalar length) rather than import its CJS
// module (which drags in a .code = 'require(...)' metadata string that would pollute naive
// greps and the bundle). No ajv runtime dep in the emitted validator.
code = code.replace(/^"use strict";/, '');

// Inline the ucs2length implementation (extracted from ajv/dist/runtime/ucs2length.js).
// Matches the exact behavior used by Ajv for minLength/maxLength on unicode strings.
const UCS2_IMPL = `const ucs2length = (str) => {
  const len = str.length;
  let length = 0;
  let pos = 0;
  let value;
  while (pos < len) {
    length++;
    value = str.charCodeAt(pos++);
    if (value >= 0xd800 && value <= 0xdbff && pos < len) {
      value = str.charCodeAt(pos);
      if ((value & 0xfc00) === 0xdc00) pos++;
    }
  }
  return length;
};
`;

// Replace the require expr (and its .default) with our local binding.
const UCS2_REQUIRE = /require\(["']ajv\/dist\/runtime\/ucs2length["']\)\.default/g;
if (UCS2_REQUIRE.test(code)) {
  code = UCS2_IMPL + code.replace(UCS2_REQUIRE, 'ucs2length');
}

// Guard: if any CommonJS require or module.exports remain, fail loudly here (will also be caught by csp-guard).
if (/require\(/.test(code) || /module\.exports/.test(code)) {
  console.error('[gen-activity-validator] ERROR: post-process failed to remove all CommonJS require/module.exports');
  console.error('  Offending snippet:', code.match(/require\([^)]*\)|module\.exports[^;]*/)?.[0]);
  process.exit(1);
}

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.writeFileSync(OUT_FILE, code, 'utf8');

console.log('[gen-activity-validator] Wrote standalone validator to', OUT_FILE);
console.log('[gen-activity-validator] Size:', code.length, 'bytes (deterministic output)');
