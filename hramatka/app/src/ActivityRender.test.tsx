/**
 * Component tests: all 9 pilot types must render from the golden fixture.
 * Derives the authoritative list from hramatka/contracts/pilot_activity_types.schema.json
 * (no second hardcoded list).
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import fs from 'fs';
import path from 'path';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';

const CONTRACTS_DIR = path.resolve(__dirname, '../../contracts');
const VENDOR_FIXTURES = path.resolve(__dirname, '../../vendor/lu.activity.v1@1.0.0-ffd54054/lu.activity.v1.fixtures.json');

function loadPilotTypes(): string[] {
  const schema = JSON.parse(fs.readFileSync(path.join(CONTRACTS_DIR, 'pilot_activity_types.schema.json'), 'utf8'));
  const arr = schema?.$defs?.pilotActivityType?.enum;
  if (!Array.isArray(arr) || arr.length !== 9) throw new Error('Failed to derive PILOT_ACTIVITY_TYPES from schema');
  return arr;
}

function loadFixtures() {
  return JSON.parse(fs.readFileSync(VENDOR_FIXTURES, 'utf8'));
}

describe('ActivityPlayer renders all 9 pilot types from golden fixture (derived)', () => {
  const types = loadPilotTypes();
  const fixtures = loadFixtures();
  const byType: Record<string, any> = {};
  for (const f of fixtures) if (f?.type) byType[f.type] = f;

  it('derives exactly the 9 frozen types', () => {
    expect(types).toHaveLength(9);
    expect(new Set(types).size).toBe(9);
  });

  for (const t of types) {
    it(`renders ${t} without placeholder or raw fallback`, () => {
      const act = byType[t];
      expect(act, `golden fixture for ${t} missing`).toBeTruthy();

      const { container } = render(
        <ActivityPlayer activity={act} isUkrainian />
      );

      // No unsupported message or raw JSON fallback
      expect(container.textContent).not.toMatch(/тип поки без віджета|unsupported|placeholder|raw-JSON/i);

      // The player wrapper or a known activity data attr from kit should exist
      const hasPlayer = container.querySelector('[data-activity-player]') || container.querySelector('[data-activity]');
      expect(hasPlayer || container.querySelector('section,button,p')).toBeTruthy();
    });
  }
});
