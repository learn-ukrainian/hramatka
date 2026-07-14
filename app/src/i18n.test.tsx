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
import { LangProvider, useT, translate, loadLang, saveLang, clearLang } from './i18n';

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
    const contentUk = screen.getByTestId('content').textContent ?? '';
    expect(contentUk.length).toBeGreaterThan(0);
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Фаза 1');
    expect(screen.getByTestId('chrome-answerkey').textContent).toBe('Ключ відповіді:');

    // toggle → EN: chrome flips, lesson content is byte-identical (content never translated)
    fireEvent.click(screen.getByTestId('toggle'));
    expect(screen.getByTestId('lang').textContent).toBe('en');
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Phase 1');
    expect(screen.getByTestId('chrome-answerkey').textContent).toBe('Answer key:');
    expect(screen.getByTestId('content').textContent).toBe(contentUk);

    // toggle back → UA restored exactly (reversible)
    fireEvent.click(screen.getByTestId('toggle'));
    expect(screen.getByTestId('lang').textContent).toBe('uk');
    expect(screen.getByTestId('chrome-phase').textContent).toBe('Фаза 1');
    expect(screen.getByTestId('content').textContent).toBe(contentUk);
  });
});
