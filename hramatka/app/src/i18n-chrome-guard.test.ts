/**
 * Regression guard (#130): user-facing Cyrillic literals must live in i18n.tsx.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  assertNoHardcodedChromeStrings,
  findHardcodedCyrillicHits,
  scanSrcDirectory,
} from './i18n-chrome-guard';

const SRC_ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '.');

describe('i18n chrome guard', () => {
  it('passes on the current src tree (no hardcoded Cyrillic UI literals)', () => {
    expect(() => assertNoHardcodedChromeStrings(SRC_ROOT)).not.toThrow();
    expect(scanSrcDirectory(SRC_ROOT)).toEqual([]);
  });

  it('fails when a deliberate hardcoded Cyrillic string is introduced (red proof)', () => {
    const probe = path.join(SRC_ROOT, '__i18n_guard_probe__.tsx');
    fs.writeFileSync(probe, 'export const BAD = "Тестовий рядок інтерфейсу";\n', 'utf8');
    try {
      const hits = findHardcodedCyrillicHits(
        fs.readFileSync(probe, 'utf8'),
        '__i18n_guard_probe__.tsx',
      );
      expect(hits).toHaveLength(1);
      expect(hits[0].line).toBe(1);
      expect(() => assertNoHardcodedChromeStrings(SRC_ROOT)).toThrow(
        /Hardcoded Cyrillic UI strings outside i18n dictionary/,
      );
    } finally {
      fs.unlinkSync(probe);
    }
  });

  it('is green again after removing the deliberate violation', () => {
    expect(scanSrcDirectory(SRC_ROOT)).toEqual([]);
  });
});
