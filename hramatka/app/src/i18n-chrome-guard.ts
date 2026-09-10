/**
 * Best-effort guard against user-facing Cyrillic string literals outside the i18n dictionary.
 *
 * Heuristic: scan .ts/.tsx under src/ for string/template literals containing Cyrillic,
 * excluding the dictionary file, tests, generated artifacts, and documented allowlists.
 *
 * Lesson CONTENT exemptions:
 *  - *.test.ts / *.test.tsx fixture data
 *  - formatLessonForClipboard() in app-helpers.ts (clipboard export stays UA)
 */
import fs from 'fs';
import path from 'path';

const CYRILLIC = /[А-Яа-яІіЇїЄєҐґ]/;

/** Files that may contain Cyrillic UI strings (the dictionary itself). */
export const ALLOWLIST_FILES = new Set([
  'i18n.tsx',
]);

/** Path suffixes always skipped. */
export const ALLOWLIST_PATH_SUFFIXES = [
  '.test.ts',
  '.test.tsx',
  '/generated/',
  'test-setup.ts',
  'activity-kit.d.ts',
  'activityValidator.d.ts',
];

/** Exact string literals that are lesson/API data, not user-facing chrome. */
export const ALLOWLIST_EXACT_LITERALS = new Set([
  'вдома', // openapi LessonBlock.mode enum
  'усно',
  'письмово',
]);

/** Function bodies that render lesson content (not chrome) — exempt by name. */
export const ALLOWLIST_FUNCTION_NAMES = new Set([
  'formatLessonForClipboard',
  'renderActivityBody',
  'renderAnswerKey',
]);

const STRING_LIT =
  /('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`)/g;

export interface ChromeGuardHit {
  file: string;
  line: number;
  text: string;
}

export function isAllowlistedSource(relPath: string): boolean {
  const base = path.basename(relPath);
  if (ALLOWLIST_FILES.has(base)) return true;
  return ALLOWLIST_PATH_SUFFIXES.some((suffix) => relPath.includes(suffix));
}

function lineNumberAt(content: string, index: number): number {
  return content.slice(0, index).split('\n').length;
}

function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (m) => ' '.repeat(m.length))
    .replace(/(^|[^:])\/\/.*$/gm, (m) => m.replace(/[^\n]/g, ' '));
}

function isInsideAllowlistedFunction(source: string, index: number): boolean {
  const head = source.slice(0, index);
  for (const name of ALLOWLIST_FUNCTION_NAMES) {
    const re = new RegExp(`function\\s+${name}\\s*\\(|export\\s+function\\s+${name}\\s*\\(`);
    const match = head.match(re);
    if (!match || match.index == null) continue;
    const open = head.indexOf('{', match.index);
    if (open < 0) continue;
    let depth = 0;
    for (let i = open; i < head.length; i++) {
      if (head[i] === '{') depth++;
      else if (head[i] === '}') depth--;
    }
    if (depth > 0) return true;
  }
  return false;
}

export function findHardcodedCyrillicHits(
  source: string,
  relPath: string,
): ChromeGuardHit[] {
  if (isAllowlistedSource(relPath)) return [];

  const stripped = stripComments(source);
  const hits: ChromeGuardHit[] = [];

  for (const match of stripped.matchAll(STRING_LIT)) {
    const lit = match[0];
    const index = match.index ?? 0;
    if (!CYRILLIC.test(lit)) continue;
    if (isInsideAllowlistedFunction(source, index)) continue;
    const inner = lit.slice(1, -1);
    if (ALLOWLIST_EXACT_LITERALS.has(inner)) continue;
    hits.push({
      file: relPath,
      line: lineNumberAt(source, index),
      text: lit.length > 80 ? `${lit.slice(0, 77)}…` : lit,
    });
  }

  return hits;
}

export function scanSrcDirectory(srcRoot: string): ChromeGuardHit[] {
  const all: ChromeGuardHit[] = [];

  function walk(dir: string) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walk(full);
        continue;
      }
      if (!/\.tsx?$/.test(entry.name)) continue;
      const rel = path.relative(srcRoot, full).replace(/\\/g, '/');
      const source = fs.readFileSync(full, 'utf8');
      all.push(...findHardcodedCyrillicHits(source, rel));
    }
  }

  walk(srcRoot);
  return all;
}

export function assertNoHardcodedChromeStrings(srcRoot: string): void {
  const hits = scanSrcDirectory(srcRoot);
  if (hits.length === 0) return;
  const report = hits.map((h) => `${h.file}:${h.line}: ${h.text}`).join('\n');
  throw new Error(
    `Hardcoded Cyrillic UI strings outside i18n dictionary (${hits.length} hit(s)):\n${report}`,
  );
}
