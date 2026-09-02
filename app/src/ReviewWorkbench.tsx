import { useState } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import ActivityEditor from './ActivityEditor';
import { DeliberateErrorBadge, DeliberateErrorList } from './DeliberateError';
import {
  type LessonDuration,
  type ReviewBlock,
  type RejectedEntry,
  type FocusStatus,
  type ActivityFeedbackEntry,
  type ActivityRegenerationEntry,
  splitReviewBlocks,
  PHASE_LABELS,
  marginStateChip,
  blockNeedsReview,
  deliberateErrors,
  focusStatusNeedsReview,
  FOCUS_STATUS_ACK_ID,
  activityTypeLabel,
  formatAnswerKeyDisplay,
  withoutTeacherOnlyAnswerKey,
} from './review-helpers';
import { useT, type ChromeKey } from './i18n';

export interface LessonResourceView {
  lesson_id: string;
  revision: number;
  warning_acknowledgements: string[];
  /** Applicable-only verdicts keyed by flagged block id (#402). */
  activity_feedback?: Record<string, ActivityFeedbackEntry>;
  activity_regenerations?: ActivityRegenerationEntry[];
  lesson: {
    title: string;
    level: string;
    duration: LessonDuration;
    focus: string | null;
    focus_status?: FocusStatus;
    anchor: { text: string };
    blocks: ReviewBlock[];
    rejected: RejectedEntry[];
    accepted: boolean;
  };
}

export interface ReviewWorkbenchProps {
  resource: LessonResourceView;
  showAnswers: boolean;
  loading: boolean;
  onMoveBlock: (blockId: string, direction: 'up' | 'down') => void;
  onRemoveBlock: (blockId: string) => void;
  onIncludeReserve: (blockId: string) => void;
  onRestoreRejected: (index: number) => void;
  onAckWarning: (blockId: string) => void;
  onSaveActivity: (blockId: string, activity: Record<string, unknown>) => void;
  onAcceptLesson: () => void;
  onReturnToDraft: () => void;
  onActivityFeedback: (blockId: string, verdict: 'good' | 'bad', comment: string | null) => void;
  onRegenerateActivity: (blockId: string, feedback: string | null) => void;
  onRetryRegeneration: (regenerationId: string) => void;
  allWarningsAcked: boolean;
  /**
   * «Друк для учня» is being prepared (#587). The workbench is the teacher's
   * surface by construction, so a student print cannot be a stylesheet that
   * paints the keys away — copy-paste of the print DOM, and any print path that
   * drops CSS, would still expose them. Under this flag every key, badge, note,
   * provenance line, engine-flagged block and rejected draft leaves the DOM,
   * which is the boundary the student run view already holds.
   */
  studentPrint?: boolean;
}

export default function ReviewWorkbench({
  resource,
  showAnswers,
  loading,
  onMoveBlock,
  onRemoveBlock,
  onIncludeReserve,
  onRestoreRejected,
  onAckWarning,
  onSaveActivity,
  onAcceptLesson,
  onReturnToDraft,
  onActivityFeedback,
  onRegenerateActivity,
  onRetryRegeneration,
  allWarningsAcked,
  studentPrint = false,
}: ReviewWorkbenchProps) {
  const { lesson, warning_acknowledgements } = resource;
  const activityFeedback = resource.activity_feedback || {};
  const [showRejected, setShowRejected] = useState(false);
  const [editingBlockId, setEditingBlockId] = useState<string | null>(null);
  const [feedbackComments, setFeedbackComments] = useState<Record<string, string>>({});
  const [feedbackReopened, setFeedbackReopened] = useState<Record<string, boolean>>({});
  const [regenerationBlockId, setRegenerationBlockId] = useState<string | null>(null);
  const [regenerationFeedback, setRegenerationFeedback] = useState('');

  const { byPhase, reserve } = splitReviewBlocks(lesson.blocks, lesson.duration);
  const acks = new Set(warning_acknowledgements);
  const regenerations = resource.activity_regenerations || [];
  const activeRegeneration = regenerations.find(
    (item) => item.status === 'queued' || item.status === 'running',
  );
  const blockIsBusy = (blockId: string) =>
    loading || activeRegeneration?.block_id === blockId;
  const latestRegenerationByBlock = new Map<string, ActivityRegenerationEntry>();
  for (const item of regenerations) {
    if (!latestRegenerationByBlock.has(item.block_id)) {
      latestRegenerationByBlock.set(item.block_id, item);
    }
  }

  // An unsupported focus is a caveat about the whole lesson, so it gets a real
  // banner rather than a slot in the rejected tray — nothing was rejected.
  const focusStatus = lesson.focus_status;
  const showFocusNotice = focusStatusNeedsReview(focusStatus);
  const focusNoticeAcked = acks.has(FOCUS_STATUS_ACK_ID);

  const { t } = useT();

  const renderBlockContent = (block: ReviewBlock) => {
    const blockBusy = blockIsBusy(block.id);
    // The editor is teacher tooling and shows the raw key fields, so a student
    // print renders the saved activity instead. `editingBlockId` is untouched:
    // the editor comes straight back when the print variant clears.
    if (!studentPrint && editingBlockId === block.id) {
      return (
        <ActivityEditor
          activity={block.activity as Record<string, unknown>}
          onSave={(act) => {
            onSaveActivity(block.id, act);
            setEditingBlockId(null);
          }}
          onCancel={() => setEditingBlockId(null)}
          disabled={blockBusy}
        />
      );
    }
    // #164: the workbench is teacher-only by construction — except for the student
    // print variant (#587), where the deliberate-error intent is exactly what must
    // not reach the page, badge and list alike.
    const intent = studentPrint ? [] : deliberateErrors(block.answer_key);
    // #587: the frozen kit prints model answers and rubrics into a teacher-guidance
    // aside on its own, so the student print hands it an activity that no longer
    // carries them.
    const activity = studentPrint ? withoutTeacherOnlyAnswerKey(block.activity) : block.activity;
    return (
      <div className="bcontent" data-activity data-activity-type={block.type}>
        {/* Marked before the activity: the point is to pre-empt reading the wrong
            Ukrainian below as an engine defect, so it must be seen first. */}
        {intent.length > 0 && <DeliberateErrorBadge />}
        <ActivityPlayer activity={activity} isUkrainian />
        {showAnswers && !studentPrint && (
          <div className="teacher-key" data-testid="teacher-answer-key">
            <strong>{t('review.answerKey')}</strong>
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
                    {formatAnswerKeyDisplay(block.answer_key, block.activity, t)}
                  </pre>
                )}
              </div>
            ) : (
              <>
                <DeliberateErrorList entries={intent} />
                <pre style={{ whiteSpace: 'pre-wrap' }}>
                  {formatAnswerKeyDisplay(block.answer_key, block.activity, t)}
                </pre>
              </>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderFeedback = (block: ReviewBlock) => {
    const blockBusy = blockIsBusy(block.id);
    // #402: the teacher judges the engine's verdict on a flagged block.
    const saved = activityFeedback[block.id];
    const reopened = feedbackReopened[block.id] === true;
    if (saved && !reopened) {
      return (
        <span className="feedback-state" data-testid="feedback-saved">
          <span className={`chip ${saved.verdict === 'good' ? 'ok' : 'bad'}`}>
            {t(saved.verdict === 'good' ? 'feedback.savedGood' : 'feedback.savedBad')}
          </span>
          <button
            type="button"
            className="link-btn"
            data-action="feedback-change"
            onClick={() => setFeedbackReopened((prev) => ({ ...prev, [block.id]: true }))}
            disabled={blockBusy}
          >
            {t('feedback.change')}
          </button>
        </span>
      );
    }
    const comment = feedbackComments[block.id] ?? saved?.comment ?? '';
    const submit = (verdict: 'good' | 'bad') => {
      onActivityFeedback(block.id, verdict, comment.trim() || null);
      setFeedbackReopened((prev) => ({ ...prev, [block.id]: false }));
    };
    return (
      <span className="feedback-form" data-testid="feedback-form">
        <span className="mnote">{t('feedback.prompt')}</span>
        <textarea
          className="feedback-comment"
          data-testid="feedback-comment"
          placeholder={t('feedback.commentPlaceholder')}
          value={comment}
          maxLength={2000}
          onChange={(event) =>
            setFeedbackComments((prev) => ({ ...prev, [block.id]: event.target.value }))
          }
          disabled={blockBusy}
        />
        <button type="button" data-action="feedback-good" onClick={() => submit('good')} disabled={blockBusy}>
          {t('feedback.good')}
        </button>
        <button type="button" data-action="feedback-bad" onClick={() => submit('bad')} disabled={blockBusy}>
          {t('feedback.bad')}
        </button>
      </span>
    );
  };

  const renderRegeneration = (block: ReviewBlock) => {
    const blockBusy = blockIsBusy(block.id);
    const eligible = /^block-[1-9][0-9]*$/.test(block.id)
      && block.edited === false
      && block.provenance?.source === 'generated';
    if (!eligible) return null;
    const latest = latestRegenerationByBlock.get(block.id);
    const isActive = latest?.status === 'queued' || latest?.status === 'running';
    if (isActive) {
      return (
        <div className="regeneration-state" data-testid="regeneration-active">
          <span className="chip muted">
            {t(latest.status === 'queued' ? 'regeneration.queued' : 'regeneration.running')}
          </span>
          <span className="mnote">{t('regeneration.keepOld')}</span>
        </div>
      );
    }
    if (latest?.status === 'failed') {
      return (
        <div className="regeneration-state" data-testid="regeneration-failed">
          <span className="chip bad">{t('regeneration.failed')}</span>
          <span className="mnote">{latest.failure_message || t('regeneration.failedFallback')}</span>
          <button
            type="button"
            data-action="regeneration-retry"
            onClick={() => onRetryRegeneration(latest.id)}
            disabled={blockBusy || Boolean(activeRegeneration)}
          >
            {t('regeneration.retry')}
          </button>
        </div>
      );
    }
    if (regenerationBlockId === block.id) {
      return (
        <div className="regeneration-form" data-testid="regeneration-form">
          <label htmlFor={`regeneration-feedback-${block.id}`}>
            {t('regeneration.feedbackLabel')}
          </label>
          <textarea
            id={`regeneration-feedback-${block.id}`}
            value={regenerationFeedback}
            maxLength={1000}
            placeholder={t('regeneration.feedbackPlaceholder')}
            onChange={(event) => setRegenerationFeedback(event.target.value)}
            disabled={blockBusy}
          />
          <span className="mtools">
            <button
              type="button"
              data-action="regeneration-submit"
              onClick={() => {
                onRegenerateActivity(block.id, regenerationFeedback.trim() || null);
                setRegenerationBlockId(null);
                setRegenerationFeedback('');
              }}
              disabled={blockBusy || Boolean(activeRegeneration)}
            >
              {t('regeneration.submit')}
            </button>
            <button
              type="button"
              data-action="regeneration-cancel"
              onClick={() => {
                setRegenerationBlockId(null);
                setRegenerationFeedback('');
              }}
              disabled={blockBusy}
            >
              {t('editor.cancel')}
            </button>
          </span>
        </div>
      );
    }
    return (
      <div className="regeneration-state">
        {latest?.status === 'succeeded' && (
          <span className="chip ok" data-testid="regeneration-succeeded">
            {t('regeneration.succeeded')}
          </span>
        )}
        <button
          type="button"
          data-action="regeneration-open"
          onClick={() => {
            setRegenerationBlockId(block.id);
            setRegenerationFeedback('');
          }}
          disabled={blockBusy || Boolean(activeRegeneration)}
        >
          {t('regeneration.open')}
        </button>
      </div>
    );
  };

  const renderMargin = (block: ReviewBlock) => {
    // The margin carries the engine's flag reason, the teacher note and the
    // provenance line. `.noprint` keeps it off paper; #587 keeps it out of the
    // page source too.
    if (studentPrint) return null;
    const blockBusy = blockIsBusy(block.id);
    const acked = acks.has(block.id);
    const chip = marginStateChip(block, acked);
    const isWarn = blockNeedsReview(block);
    const isFlagged = block.quality === 'engine_flagged';
    // #113 deliberately clears a warning acknowledgement after an edit. An edited
    // visible warning must therefore remain acknowledgeable, not become a dead end.
    const showAck = isWarn && !acked;

    return (
      <div className="dmargin noprint">
        <span className={`chip ${chip.className}`}>{t(chip.key as ChromeKey)}</span>
        {/* #402: the engine's verdict, engine-authored UK, rendered verbatim. */}
        {isFlagged && block.flag_reason_uk && (
          <span className="mnote flagged-badge" lang="uk" data-testid="flagged-badge">
            {block.flag_reason_uk}
          </span>
        )}
        {block.note && <span className="mnote">{block.note}</span>}
        {block.provenance && <span className="mnote">{t('review.source')}{block.provenance.source || '—'} · {t('review.generator')}{block.provenance.generator || '—'}</span>}
        {block.provenance && <span className="mnote">{t('review.checks')}{block.provenance.gates?.length ? block.provenance.gates.join(', ') : '—'}</span>}
        {block.provenance?.external_options && <span className="mnote">{t('review.external')}</span>}
        <span className="mtools">
          <button type="button" title={t('review.edit')} data-action="b-edit" onClick={() => setEditingBlockId(block.id)} disabled={blockBusy}>✎</button>
          <button type="button" title={t('review.up')} data-action="b-up" onClick={() => onMoveBlock(block.id, 'up')} disabled={blockBusy}>↑</button>
          <button type="button" title={t('review.down')} data-action="b-down" onClick={() => onMoveBlock(block.id, 'down')} disabled={blockBusy}>↓</button>
          <button type="button" title={t('review.remove')} data-action="b-del" onClick={() => onRemoveBlock(block.id)} disabled={blockBusy}>✕</button>
          {showAck && (
            <button type="button" className="accept ack-btn" data-action="b-accept" onClick={() => onAckWarning(block.id)} disabled={blockBusy}>
              {t('review.ack')}
            </button>
          )}
        </span>
        {isFlagged && renderFeedback(block)}
        {renderRegeneration(block)}
      </div>
    );
  };

  const renderPlannedBlock = (block: ReviewBlock) => (
    <div
      key={block.id}
      id={`blk${block.id}`}
      className={`dblock block ${block.mark === 'warn' ? 'warn' : 'ok'} ${block.quality === 'engine_flagged' ? 'flagged' : ''} ${block.edited ? 'edited' : ''} ${editingBlockId === block.id ? 'editing' : ''}`}
      data-block-id={block.id}
    >
      {/* #590: student run omits `.block-meta` type/mode chrome; student print matches. */}
      {!studentPrint && (
        <span className="type">
          {t(activityTypeLabel(block.type) as ChromeKey)}
          {block.mode && <span className="chip muted mode-chip">{block.mode}</span>}
        </span>
      )}
      {renderBlockContent(block)}
      {renderMargin(block)}
    </div>
  );

  return (
    <div className="review-workbench" data-testid="review-workbench">
      <div className="banner honest noprint">
        <span className="ic">📖</span>
        <span><b>Факти перевірте.</b> {t('review.banner')}</span>
      </div>

      {/* #587: the banner is `.noprint`, but it repeats the same teacher-only
          caveat as the omitted sheet line, so a student print drops both. The
          remaining `.noprint` chrome — the honesty banner, the duration toolbar,
          the accept bar — carries no key, judgement or note about this lesson. */}
      {showFocusNotice && !studentPrint && (
        <div className="banner honest focus-notice noprint" data-testid="focus-notice">
          <span className="ic">⚠</span>
          <span>
            <b>{t('review.focusNotice')}</b>
            <span className="mnote focus-notice-requested">
              {t('review.focusNoticeRequested')}«{focusStatus!.requested}»
            </span>
            {/* Engine-authored, learner-facing: rendered verbatim, never translated. */}
            <p className="focus-notice-body" lang="uk" data-testid="focus-notice-body">
              {focusStatus!.notice_uk}
            </p>
            {focusNoticeAcked ? (
              <span className="chip ok" data-testid="focus-notice-acked">
                {t('review.focusNoticeAcked')}
              </span>
            ) : (
              <button
                type="button"
                className="accept ack-btn"
                data-action="focus-notice-accept"
                onClick={() => onAckWarning(FOCUS_STATUS_ACK_ID)}
                disabled={loading}
              >
                {t('review.focusNoticeAck')}
              </button>
            )}
          </span>
        </div>
      )}

      <div className="noprint review-toolbar">
        <p className="sub">{t('review.sub')}</p>
        <span className="duration-picker" data-testid="review-duration-readonly">
          <span className="duration-label">{t('review.durationLabel')}</span>
          <span className="choice on">{t('review.durationChip', { minutes: lesson.duration })}</span>
          <span className="duration-hint">{t('review.durationReadOnly')}</span>
        </span>
      </div>

      <div className="paper review-paper">
        <p className="docsheet-title">{lesson.title}</p>
        <p className="docsheet-meta">{t('review.docsheetMeta', { level: lesson.level, duration: lesson.duration })}</p>

        <h4 className="dochead">
          <span className="pn">☰</span>{t('anchor.readingHead')}
        </h4>
        <div className="anchorbody">{lesson.anchor.text}</div>

        {([1, 2, 3] as const).map((phase) => {
          const label = PHASE_LABELS[phase];
          // #402/#587: an engine-flagged block is teacher-review-only, so the
          // student print drops it exactly as the student run view does — and a
          // phase left with nothing keeps the teacher's empty-phase notice off
          // the student's sheet by rendering no section at all.
          const visible = studentPrint
            ? byPhase[phase].visible.filter((block) => block.quality !== 'engine_flagged')
            : byPhase[phase].visible;
          if (studentPrint && visible.length === 0) return null;
          return (
            <section key={phase} className="review-phase" data-phase={phase}>
              <h4 className="dochead">
                <span className="pn">{t(label.pnKey)}</span>
                {t(label.titleKey)}
                <span className="pd">{t('review.phaseDuration', { minutes: label.pd[lesson.duration] })}</span>
              </h4>
              {visible.length === 0 && (
                <div className="dblock block empty-phase">
                  <p>{t('review.emptyPhase')}</p>
                </div>
              )}
              {visible.map(renderPlannedBlock)}
            </section>
          );
        })}
        <p className="docsheet-footer" data-testid="honesty-footer">
          {t('print.honestyFooter')}
        </p>
        {/* The banner above is screen chrome and prints away with the rest of it,
            so the caveat rides the sheet to survive onto the teacher's paper.
            #587: it is addressed to the teacher — it explains what the engine
            could not ground — so it stays off the student's sheet entirely. */}
        {showFocusNotice && !studentPrint && (
          <p
            className="docsheet-footer focus-notice-print"
            data-testid="focus-notice-print"
            lang="uk"
          >
            {t('review.focusNotice')}: {focusStatus!.notice_uk}
          </p>
        )}
      </div>

      {reserve.length > 0 && (
        <section className="reserve-tray" data-testid="reserve-tray">
          {/* #590: reserve heading is teacher planning chrome; blocks stay student-visible. */}
          {!studentPrint && (
            <p className="reserve-head">
              <b>{t('review.reserveHead', { duration: lesson.duration })}</b>
            </p>
          )}
          {reserve.map((block) => (
            <div key={block.id} className="dblock block reserve-block" data-block-id={block.id}>
              {!studentPrint && (
                <span className="type">
                  {t(activityTypeLabel(block.type) as ChromeKey)}
                  {block.mode && <span className="chip muted mode-chip">{block.mode}</span>}
                </span>
              )}
              <div className="bcontent" data-activity data-activity-type={block.type}>
                <ActivityPlayer
                  activity={studentPrint ? withoutTeacherOnlyAnswerKey(block.activity) : block.activity}
                  isUkrainian
                />
              </div>
              {!studentPrint && (
              <div className="dmargin noprint">
                <span className="mtools">
                  <button type="button" data-action="b-include" onClick={() => onIncludeReserve(block.id)} disabled={blockIsBusy(block.id)}>
                    {t('review.include')}
                  </button>
                </span>
                {renderRegeneration(block)}
              </div>
              )}
            </div>
          ))}
        </section>
      )}

      {/* #587: a rejected draft is the teacher's audit trail, not lesson content —
          the student run view never shows it, and neither does the student print. */}
      {lesson.rejected.length > 0 && !studentPrint && (
        <section className="rejected-tray" data-testid="rejected-tray">
          <p className="rejected-head">
            {(() => {
              const count = lesson.rejected.length;
              return t(count === 1 ? 'review.rejectedHeadOne' : 'review.rejectedHeadMany', { count });
            })()}
            <button type="button" className="link-btn" data-action="toggle-rejected" onClick={() => setShowRejected((v) => !v)}>
              {t(showRejected ? 'review.hide' : 'review.show')}
            </button>{' '}
            {t('review.rejectedNote')}
          </p>
          {showRejected && lesson.rejected.map((entry, i) => (
            <div key={i} className="dblock block rejected-block" data-rejected-index={i}>
              <span className="type rejected-type">✕ {t(activityTypeLabel(entry.type) as ChromeKey)}</span>
              <div className="bcontent">
                {entry.activity && typeof entry.activity === 'object' && (entry.activity as Record<string, unknown>).type ? (
                  <ActivityPlayer activity={entry.activity} isUkrainian />
                ) : (
                  <p className="hint">{t('review.noWidget')}</p>
                )}
              </div>
              <div className="dmargin noprint">
                <span className="chip bad">{t('review.rejectedChip')}</span>
                <span className="mnote">{entry.reason}</span>
                <span className="mtools">
                  <button type="button" data-action="b-restore" onClick={() => onRestoreRejected(i)} disabled={loading}>
                    {t('review.restore')}
                  </button>
                </span>
              </div>
            </div>
          ))}
        </section>
      )}

      <div className="accept-bar noprint">
        {!allWarningsAcked && (
          <div className="warn-note">{t('review.acceptWarn')}</div>
        )}
        <button type="button" className="btn primary" data-action="accept-lesson" onClick={onAcceptLesson} disabled={loading || !allWarningsAcked || lesson.accepted}>
          {t('review.accept')}
        </button>
        <button type="button" className="btn ghost" data-action="save-draft" onClick={onReturnToDraft} disabled={loading || !lesson.accepted}>
          {t('review.saveDraft')}
        </button>
      </div>
    </div>
  );
}
