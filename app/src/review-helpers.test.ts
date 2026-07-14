import { describe, it, expect } from 'vitest';
import {
  splitReviewBlocks,
  validateActivityDocument,
  regenerateAnswerKey,
  REVIEW_PHASE_BUDGETS,
  blockNeedsReview,
  marginStateChip,
  type ReviewBlock,
} from './review-helpers';
import activityFixtures from '@learn-ukrainian/activity-kit/fixtures';

function makeBlock(id: string, phase: 1 | 2 | 3): ReviewBlock {
  return {
    id,
    phase,
    type: 'true-false',
    mode: 'усно',
    activity: { id, type: 'true-false', title: 'T', level: 'b1', payload: { type: 'true-false', instruction: 'x', items: [{ statement: 's', correct: true }] }, answer_key: { items: [{ index: 0, correct: true }] } },
    answer_key: {},
    mark: 'ok',
    note: null,
    edited: false,
  };
}

describe('blockNeedsReview / marginStateChip honesty flags', () => {
  const baseBlock: ReviewBlock = {
    id: 'block-cloze',
    phase: 1,
    type: 'cloze',
    mode: 'письмово',
    activity: {},
    answer_key: {},
    mark: 'ok',
    note: null,
    edited: false,
  };

  it('treats external_options as review-required even when mark is stale ok', () => {
    const block: ReviewBlock = {
      ...baseBlock,
      provenance: { source: 'generated', generator: 'gemma', gates: [], external_options: true },
    };
    expect(blockNeedsReview(block)).toBe(true);
    expect(marginStateChip(block, false)).toEqual({ className: 'warn', key: 'chip.look' });
  });

  it('shows confirmed chip only after acknowledgement', () => {
    const block: ReviewBlock = { ...baseBlock, mark: 'warn' };
    expect(marginStateChip(block, true)).toEqual({ className: 'ok', key: 'chip.confirmed' });
  });
});

describe('splitReviewBlocks', () => {
  const blocks: ReviewBlock[] = [
    makeBlock('p1a', 1), makeBlock('p1b', 1), makeBlock('p1c', 1),
    makeBlock('p2a', 2), makeBlock('p2b', 2), makeBlock('p2c', 2), makeBlock('p2d', 2), makeBlock('p2e', 2),
    makeBlock('p3a', 3), makeBlock('p3b', 3), makeBlock('p3c', 3),
  ];

  it('splits 45-min plan as 3/4/1 visible per phase', () => {
    const { visible, reserve, byPhase } = splitReviewBlocks(blocks, 45);
    expect(visible).toHaveLength(8);
    expect(reserve).toHaveLength(3);
    expect(byPhase[1].visible).toHaveLength(REVIEW_PHASE_BUDGETS[45][1]);
    expect(byPhase[2].visible).toHaveLength(REVIEW_PHASE_BUDGETS[45][2]);
    expect(byPhase[3].visible).toHaveLength(REVIEW_PHASE_BUDGETS[45][3]);
    expect(byPhase[1].reserve).toEqual([]);
  });

  it('keeps all blocks in visible or reserve (nothing disappears)', () => {
    for (const duration of [45, 60, 90] as const) {
      const { visible, reserve } = splitReviewBlocks(blocks, duration);
      expect(visible.length + reserve.length).toBe(blocks.length);
    }
  });
});

describe('validateActivityDocument', () => {
  it('accepts a valid true-false activity', () => {
    const activity = {
      id: 'a1',
      type: 'true-false',
      title: 'Тест',
      level: 'b1',
      payload: {
        type: 'true-false',
        instruction: 'Оберіть.',
        items: [{ statement: 'Це правда.', correct: true }],
      },
      answer_key: { items: [{ index: 0, correct: true }] },
      provenance: { source: 'test', generator: 'vitest', gates: ['schema'] },
    };
    expect(validateActivityDocument(activity).valid).toBe(true);
  });

  it('rejects empty instruction', () => {
    const activity = {
      id: 'a1',
      type: 'true-false',
      title: 'Тест',
      level: 'b1',
      payload: { type: 'true-false', instruction: '  ', items: [{ statement: 's', correct: true }] },
      answer_key: { items: [{ index: 0, correct: true }] },
    };
    const result = validateActivityDocument(activity);
    expect(result.valid).toBe(false);
    expect(result.errors.length).toBeGreaterThan(0);
  });

  it('rejects short-writing without prompt', () => {
    const activity = {
      id: 'sw',
      type: 'short-writing',
      title: 'Письмо',
      level: 'b1',
      payload: { type: 'short-writing', prompt: '' },
      answer_key: { guidance: 'x' },
      provenance: { source: 'test', generator: 'vitest', gates: ['schema'] },
    };
    expect(validateActivityDocument(activity).valid).toBe(false);
  });

  it('accepts every shipped pilot fixture through the actual vendored schema', () => {
    expect(activityFixtures).toHaveLength(9);
    for (const fixture of activityFixtures) {
      expect(validateActivityDocument(fixture).valid, fixture.type).toBe(true);
    }
  });

  it('enforces contract fields the old hand-written validator missed', () => {
    const matchUp = structuredClone(activityFixtures.find((fixture) => fixture.type === 'match-up')!);
    const matchUpPayload = matchUp.payload as Record<string, unknown>;
    matchUpPayload.pairs = [(matchUpPayload.pairs as unknown[])[0]];
    expect(validateActivityDocument(matchUp).valid).toBe(false);

    const missingProvenance = structuredClone(activityFixtures[0]);
    delete (missingProvenance as Record<string, unknown>).provenance;
    expect(validateActivityDocument(missingProvenance).valid).toBe(false);

    const extraPayloadField = structuredClone(activityFixtures[0]);
    (extraPayloadField.payload as Record<string, unknown>).unexpected = 'nope';
    expect(validateActivityDocument(extraPayloadField).valid).toBe(false);
  });
});

describe('regenerateAnswerKey', () => {
  it('keeps all fixture activities schema-valid while deriving their keys', () => {
    for (const fixture of activityFixtures) {
      expect(validateActivityDocument(regenerateAnswerKey(structuredClone(fixture))).valid, fixture.type).toBe(true);
    }
  });

  it('updates deterministic keys without retaining stale answers', () => {
    const fillIn = structuredClone(activityFixtures.find((fixture) => fixture.type === 'fill-in')!);
    const fillInPayload = fillIn.payload as Record<string, unknown>;
    ((fillInPayload.items as unknown[])[0] as Record<string, unknown>).answer = 'оновлена форма';
    const rebuilt = regenerateAnswerKey(fillIn);
    expect(rebuilt.answer_key).toEqual({ items: ['оновлена форма'] });
    expect(validateActivityDocument(rebuilt).valid).toBe(true);
  });
});
