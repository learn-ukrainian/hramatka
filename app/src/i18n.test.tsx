/**
 * i18n translation layer:
 *  - key-based translation, interpolation, uk-default + key fallback
 *  - persistence + session-boundary reset
 *  - CONTENT INVARIANCE (item 3): a rendered lesson's content strings are unchanged
 *    when chrome=EN, while chrome flips — and the toggle is reversible.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import fs from 'fs';
import path from 'path';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import { LangProvider, useT, translate, loadLang, saveLang, clearLang, recoveryBodyKey } from './i18n';

const VENDOR_FIXTURES = path.resolve(
  __dirname,
  '../../vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.fixtures.json',
);
function loadFixtures(): any[] {
  return JSON.parse(fs.readFileSync(VENDOR_FIXTURES, 'utf8'));
}

/** Mirrors the app's block render: chrome via t(), lesson content via ActivityPlayer(isUkrainian). */
function Harness({ activity }: { activity: any }) {
  const { t, lang, toggleLang } = useT();
  return (
    <div>
      <button data-testid="toggle" onClick={toggleLang}>toggle</button>
      <span data-testid="lang">{lang}</span>
      <h3 data-testid="chrome-phase">{t('blocks.phase', { phase: 1 })}</h3>
      <span data-testid="chrome-answerkey">{t('blocks.answerKey')}</span>
      <div data-testid="content"><ActivityPlayer activity={activity} isUkrainian /></div>
    </div>
  );
}

describe('i18n translation layer', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('defaults to Ukrainian when nothing is persisted', () => {
    expect(loadLang()).toBe('uk');
  });

  it('translate resolves per language, interpolates params, and falls back to the key', () => {
    expect(translate('uk', 'brand')).toBe('Граматка');
    expect(translate('en', 'brand')).toBe('Hramatka');
    expect(translate('uk', 'blocks.phase', { phase: 2 })).toBe('Фаза 2');
    expect(translate('en', 'blocks.phase', { phase: 2 })).toBe('Phase 2');
    // status chip labels stay UA-identical to app-helpers.statusLabel in UA
    expect(translate('uk', 'status.ready')).toBe('готово');
    expect(translate('en', 'status.ready')).toBe('ready');
    // unknown key falls back to the key string itself (never throws)
    expect(translate('en', 'no.such.key' as any)).toBe('no.such.key');
  });

  it('persists the choice and resetLang (clearLang) returns to default UA', () => {
    saveLang('en');
    expect(loadLang()).toBe('en');
    clearLang();
    expect(loadLang()).toBe('uk');
  });

  it('maps each failure_code to distinct recovery body copy (#185)', () => {
    const cases: Array<{ code: string | null; mustIncludeUk: string; mustNotIncludeUk?: string }> = [
      {
        code: 'lesson_floor_unmet',
        mustIncludeUk: 'Спробуйте ще раз',
        mustNotIncludeUk: 'Опорного матеріалу',
      },
      {
        code: 'insufficient_anchor_capacity',
        mustIncludeUk: 'Опорного матеріалу недостатньо',
        mustNotIncludeUk: '2–3',
      },
      {
        code: 'engine_unavailable',
        mustIncludeUk: 'перевантажений',
      },
      {
        code: 'provider_unavailable',
        mustIncludeUk: 'Постачальник не зміг завершити',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'bake_timeout',
        mustIncludeUk: 'Час очікування',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'lesson_schema_invalid',
        mustIncludeUk: 'Спробуйте ще раз',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'worker_restarted',
        mustIncludeUk: 'Спробуйте ще раз',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'unknown_safe_failure',
        mustIncludeUk: 'Спробуйте ще раз',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'generation_failed',
        mustIncludeUk: 'відповідь генератора',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: 'no_eligible_activities',
        mustIncludeUk: 'підібрати вправи',
        mustNotIncludeUk: 'перевантажений',
      },
      {
        code: null,
        mustIncludeUk: 'перевантажений', // generic fallback keeps prior overload framing
      },
      {
        code: 'totally_unknown_code',
        mustIncludeUk: 'перевантажений',
      },
    ];

    for (const c of cases) {
      const key = recoveryBodyKey(c.code);
      const uk = translate('uk', key);
      const en = translate('en', key);
      expect(uk, `uk for ${c.code}`).toContain(c.mustIncludeUk);
      if (c.mustNotIncludeUk) {
        expect(uk, `uk for ${c.code} should not claim overload`).not.toContain(c.mustNotIncludeUk);
      }
      // dual-lang: EN is non-empty and not the raw key
      expect(en.length).toBeGreaterThan(10);
      expect(en).not.toBe(key);
      // Only a deterministic anchor-capacity result may ask for more source
      // material; generic post-generation floors must keep the retry wording.
      if (c.code === 'insufficient_anchor_capacity') {
        expect(uk).not.toMatch(/поганий|тонк/i);
        expect(uk).toContain('повного уроку');
        expect(uk).toContain('довший і різноманітніший текст із конкретними деталями');
        expect(uk).toContain('ваш текст уже збережено');
        expect(uk).not.toMatch(/2[–-]3/);
        expect(en.toLowerCase()).toContain('supported material');
        expect(en.toLowerCase()).toContain('complete lesson');
        expect(en.toLowerCase()).toContain('longer, more varied source with concrete details');
        expect(en.toLowerCase()).toContain('your text is already saved');
        expect(en).not.toMatch(/2[–-]3/);
      }
      if (c.code === 'lesson_floor_unmet') {
        expect(uk).toContain('Спробуйте ще раз');
        expect(uk).not.toMatch(/опорного матеріалу|довший|різноманітніший/i);
        expect(en.toLowerCase()).toContain('try again');
        expect(en.toLowerCase()).not.toContain('source does not contain');
      }
    }

    // Source-capacity, post-generation floor, and overload remain distinct.
    expect(translate('uk', recoveryBodyKey('insufficient_anchor_capacity'))).not.toBe(
      translate('uk', recoveryBodyKey('lesson_floor_unmet')),
    );
    expect(translate('uk', recoveryBodyKey('lesson_floor_unmet'))).not.toBe(
      translate('uk', recoveryBodyKey('engine_unavailable')),
    );
  });

  it('keeps lesson CONTENT unchanged when chrome=EN, while chrome flips (reversible)', () => {
    const activity = loadFixtures().find((f) => f?.type);
    expect(activity, 'need at least one golden fixture').toBeTruthy();

    render(
      <LangProvider>
        <Harness activity={activity} />
      </LangProvider>,
    );

    // default Ukrainian chrome + Ukrainian content
    expect(screen.getByTestId('lang').textContent).toBe('uk');
    expect(document.documentElement.lang).toBe('uk');
    const contentUk = screen.getByTestId('content').textContent ?? '';
    expect(contentUk.length).toBeGreaterThan(0);
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Фаза 1');
    expect(screen.getByTestId('chrome-answerkey').textContent).toBe('Ключ відповіді:');

    // toggle → EN: chrome flips, lesson content is byte-identical (content never translated)
    fireEvent.click(screen.getByTestId('toggle'));
    expect(screen.getByTestId('lang').textContent).toBe('en');
    expect(document.documentElement.lang).toBe('en');
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Phase 1');
    expect(screen.getByTestId('chrome-answerkey').textContent).toBe('Answer key:');
    expect(screen.getByTestId('content').textContent).toBe(contentUk);

    // toggle back → UA restored exactly (reversible)
    fireEvent.click(screen.getByTestId('toggle'));
    expect(screen.getByTestId('lang').textContent).toBe('uk');
    expect(document.documentElement.lang).toBe('uk');
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Фаза 1');
    expect(screen.getByTestId('content').textContent).toBe(contentUk);
  });
});
