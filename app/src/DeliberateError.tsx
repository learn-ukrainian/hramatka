/**
 * Teacher-only «Навмисна помилка» affordance for error-correction blocks (#164).
 *
 * Error-correction activities print deliberately wrong Ukrainian. Teachers read that as
 * an engine defect (Sol pedagogy advisory, 2026-07-14; observed live in the Alona demo),
 * so review marks the intent explicitly.
 *
 * STUDENTS MUST NEVER SEE EITHER EXPORT. Both are gated by the caller on review mode and
 * render nothing without engine intent metadata; neither is CSS-hidden, so a student
 * render omits them from the DOM outright rather than merely painting them away.
 *
 * The badge answers "is this a bug?" and so stays visible even with answers hidden. The
 * list marks *where* the error is, which is answer-revealing, so callers gate it behind
 * `showAnswers` alongside the rest of the key.
 */
import { useT } from './i18n';
import { splitOnDeliberateError, type DeliberateError } from './review-helpers';

/** Amber intent badge. Chrome (dual-lang); carries no lesson content. */
export function DeliberateErrorBadge() {
  const { t } = useT();
  return (
    <span className="deliberate-error-badge teacher-only" data-testid="deliberate-error-badge">
      {t('blocks.deliberateError')}
    </span>
  );
}

/**
 * Each deliberately-broken sentence with its erroneous form marked, then the correction.
 *
 * The sentences are lesson CONTENT and stay Ukrainian in both chrome languages. The
 * `→` separator is a symbol rather than a word so this adds no untested UA morphology.
 */
export function DeliberateErrorList({ entries }: { entries: DeliberateError[] }) {
  const { t } = useT();
  if (entries.length === 0) return null;
  return (
    <ul className="deliberate-error-list teacher-only" data-testid="deliberate-error-list">
      {entries.map((entry, index) => {
        const { before, error, after } = splitOnDeliberateError(entry);
        return (
          <li key={index} className="deliberate-error-item">
            <span lang="uk">
              {before}
              <mark
                className="deliberate-error-mark"
                data-testid="deliberate-error-mark"
                title={t('blocks.deliberateError')}
              >
                {error}
              </mark>
              {after}
            </span>
            <span aria-hidden="true" className="deliberate-error-arrow"> → </span>
            <span lang="uk" className="deliberate-error-correction">{entry.correction}</span>
          </li>
        );
      })}
    </ul>
  );
}
