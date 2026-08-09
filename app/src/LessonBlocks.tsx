/**
 * Phase-grouped lesson block renderer, shared by the teacher review/conduct views and
 * the student `run` view.
 *
 * Extracted verbatim from `App.tsx`'s inner `renderBlocks` closure (#164). It was
 * previously unreachable from tests, so the teacher/student split — the thing that
 * decides whether a student sees answer keys — had no coverage at all. The split is a
 * correctness boundary, not a cosmetic one, so it now lives somewhere it can be proven.
 *
 * The single rule this file exists to keep: everything gated on `viewMode === 'review'`
 * or `!isStudentView` is TEACHER-ONLY and must never reach the student render.
 */
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import { DeliberateErrorBadge, DeliberateErrorList } from './DeliberateError';
import { useT } from './i18n';
import { deliberateErrors, formatAnswerKeyDisplay } from './review-helpers';
import StudentActivityPlayer from './StudentActivityPlayer';

export type LessonViewMode = 'review' | 'run' | 'conduct';

export interface LessonBlocksBlock {
  id: string;
  phase: 1 | 2 | 3;
  type: string;
  mode: string;
  activity: any; // LuActivityV1 shape from kit
  answer_key: string | object | null;
  mark: 'ok' | 'warn';
  note: string | null;
  edited: boolean;
  provenance?: any;
  /** #402: engine verdict; `flag_reason_uk` is engine-authored UK, rendered verbatim. */
  quality?: 'engine_ok' | 'engine_flagged';
  flag_reason_uk?: string | null;
}

export interface LessonBlocksProps {
  blocks: LessonBlocksBlock[];
  viewMode: LessonViewMode;
  showAnswers: boolean;
  /** Block ids whose warning is acknowledged (server-side plus optimistic local). */
  acknowledgedIds: string[];
  onAck: (blockId: string) => void;
  loading: boolean;
}

export default function LessonBlocks({
  blocks,
  viewMode,
  showAnswers,
  acknowledgedIds,
  onAck,
  loading,
}: LessonBlocksProps) {
  const { t } = useT();
  const isStudentView = viewMode === 'run';
  const phases: Record<number, LessonBlocksBlock[]> = { 1: [], 2: [], 3: [] };
  blocks.forEach(b => {
    // #402: an engine-flagged block is teacher-review-only. The student render
    // stays byte-for-byte what it was before flagging existed (N-1 exercises),
    // exactly as a dropped slot was never visible to a student.
    if (isStudentView && b.quality === 'engine_flagged') return;
    if (phases[b.phase]) phases[b.phase].push(b);
  });

  return (
    <>
      {[1, 2, 3].map(phase => {
        const bs = phases[phase];
        if (!bs.length) return null;
        return (
          <section key={phase} className="phase">
            <h3>{t('blocks.phase', { phase })}</h3>
            {bs.map(block => {
              const isWarn = block.mark === 'warn';
              const isFlagged = block.quality === 'engine_flagged';
              const acked = acknowledgedIds.includes(block.id);
              const showKey = viewMode === 'review' && showAnswers;
              const typeChip = isWarn ? 'warn' : 'info';
              // #164: triples live on the outer block key because the activity envelope is frozen.
              const corrections = block.type === 'error-correction' ? deliberateErrors(block.answer_key) : [];
              const intent = viewMode === 'review' ? corrections : [];
              return (
                <div
                  key={block.id}
                  className={`block ${isStudentView ? 'student-block' : ''} ${isWarn && !isStudentView ? 'warn' : 'ok'} ${isFlagged && !isStudentView ? 'flagged' : ''} ${block.edited && !isStudentView ? 'edited' : ''}`}
                  data-testid={isStudentView ? 'student-block' : undefined}
                >
                  {!isStudentView && (
                    <div className="block-meta">
                      <span className={`chip ${typeChip}`}>{block.type}</span>
                      <span className="mode">{block.mode}</span>
                      {isWarn && <span className="warn-badge">{t('blocks.warnBadge')}</span>}
                      {/* #402: engine-authored UK verdict, rendered verbatim. */}
                      {isFlagged && block.flag_reason_uk && (
                        <span className="flagged-badge" lang="uk">{block.flag_reason_uk}</span>
                      )}
                      {intent.length > 0 && <DeliberateErrorBadge />}
                      {block.provenance?.external_options && <span className="prov">{t('blocks.externalOptions')}</span>}
                    </div>
                  )}

                  {/* REAL WIDGET — zero fallback (container styled only; kit kept untouched) */}
                  <div className="activity-wrapper" data-activity data-activity-type={block.type}>
                    {isStudentView && <StudentActivityPlayer activity={block.activity} corrections={corrections} />}
                    {!isStudentView && <ActivityPlayer
                      activity={block.activity}
                      isUkrainian={true}
                      // onComplete omitted for teacher review/run
                    />}
                  </div>

                  {showKey && (
                    <div className="teacher-key teacher-only" data-testid="teacher-answer-key">
                      <strong>{t('blocks.answerKey')}</strong>
                      {block.type === 'short-writing' ? (
                        <div className="short-writing-details" style={{ marginTop: '0.25rem' }}>
                          {(block.activity as any)?.answer_key?.rubric && (
                            <div className="rubric-block" style={{ marginBottom: '0.25rem' }}>
                              <strong>{t('editor.field.rubric')}:</strong>
                              <pre style={{ marginTop: '0.125rem', whiteSpace: 'pre-wrap' }}>
                                {String((block.activity as any).answer_key.rubric)}
                              </pre>
                            </div>
                          )}
                          {(block.activity as any)?.answer_key?.model_answer && (
                            <div className="model-answer-block">
                              <strong>{t('editor.field.modelAnswer')}:</strong>
                              <pre style={{ marginTop: '0.125rem', whiteSpace: 'pre-wrap' }}>
                                {String((block.activity as any).answer_key.model_answer)}
                              </pre>
                            </div>
                          )}
                          {!(block.activity as any)?.answer_key?.rubric && !(block.activity as any)?.answer_key?.model_answer && (
                            <pre style={{ whiteSpace: 'pre-wrap' }}>
                              {formatAnswerKeyDisplay(block.answer_key, block.activity as Record<string, unknown>, t)}
                            </pre>
                          )}
                        </div>
                      ) : (
                        <>
                          {/* #164: which form was broken on purpose, above the corrected sentences. */}
                          <DeliberateErrorList entries={intent} />
                          <pre style={{ whiteSpace: 'pre-wrap' }}>
                            {formatAnswerKeyDisplay(block.answer_key, block.activity as Record<string, unknown>, t)}
                          </pre>
                        </>
                      )}
                      {block.note && <div className="note">{t('blocks.note')}{block.note}</div>}
                      {block.provenance && (
                        <div className="prov-detail">{t('blocks.provenance', { source: block.provenance.source, generator: block.provenance.generator })}</div>
                      )}
                    </div>
                  )}

                  {viewMode === 'review' && isWarn && !acked && (
                    <button
                      className="ack-btn btn"
                      onClick={() => onAck(block.id)}
                      disabled={loading}
                      data-testid="warning-ack-btn"
                    >
                      {t('blocks.ackBtn')}
                    </button>
                  )}
                  {viewMode === 'review' && isWarn && acked && (
                    <span className="acked">{t('blocks.acked')}</span>
                  )}
                </div>
              );
            })}
          </section>
        );
      })}
    </>
  );
}
