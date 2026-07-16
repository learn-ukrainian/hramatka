import { useState } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import ActivityEditor from './ActivityEditor';
import {
  type LessonDuration,
  type ReviewBlock,
  type RejectedEntry,
  type FocusStatus,
  splitReviewBlocks,
  PHASE_LABELS,
  marginStateChip,
  blockNeedsReview,
  focusStatusNeedsReview,
  FOCUS_STATUS_ACK_ID,
  activityTypeLabel,
  formatAnswerKeyDisplay,
} from './review-helpers';
import { useT, type ChromeKey } from './i18n';

export interface LessonResourceView {
  lesson_id: string;
  revision: number;
  warning_acknowledgements: string[];
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
  onDurationChange: (duration: LessonDuration) => void;
  onMoveBlock: (blockId: string, direction: 'up' | 'down') => void;
  onRemoveBlock: (blockId: string) => void;
  onIncludeReserve: (blockId: string) => void;
  onRestoreRejected: (index: number) => void;
  onAckWarning: (blockId: string) => void;
  onSaveActivity: (blockId: string, activity: Record<string, unknown>) => void;
  onAcceptLesson: () => void;
  onReturnToDraft: () => void;
  allWarningsAcked: boolean;
}

export default function ReviewWorkbench({
  resource,
  showAnswers,
  loading,
  onDurationChange,
  onMoveBlock,
  onRemoveBlock,
  onIncludeReserve,
  onRestoreRejected,
  onAckWarning,
  onSaveActivity,
  onAcceptLesson,
  onReturnToDraft,
  allWarningsAcked,
}: ReviewWorkbenchProps) {
  const { lesson, warning_acknowledgements } = resource;
  const [showRejected, setShowRejected] = useState(false);
  const [editingBlockId, setEditingBlockId] = useState<string | null>(null);

  const { byPhase, reserve } = splitReviewBlocks(lesson.blocks, lesson.duration);
  const acks = new Set(warning_acknowledgements);

  // An unsupported focus is a caveat about the whole lesson, so it gets a real
  // banner rather than a slot in the rejected tray — nothing was rejected.
  const focusStatus = lesson.focus_status;
  const showFocusNotice = focusStatusNeedsReview(focusStatus);
  const focusNoticeAcked = acks.has(FOCUS_STATUS_ACK_ID);

  const { t } = useT();

  const renderBlockContent = (block: ReviewBlock) => {
    if (editingBlockId === block.id) {
      return (
        <ActivityEditor
          activity={block.activity as Record<string, unknown>}
          onSave={(act) => {
            onSaveActivity(block.id, act);
            setEditingBlockId(null);
          }}
          onCancel={() => setEditingBlockId(null)}
          disabled={loading}
        />
      );
    }
    return (
      <div className="bcontent" data-activity-type={block.type}>
        <ActivityPlayer activity={block.activity} isUkrainian />
        {showAnswers && (
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
              <pre style={{ whiteSpace: 'pre-wrap' }}>
                {formatAnswerKeyDisplay(block.answer_key, block.activity, t)}
              </pre>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderMargin = (block: ReviewBlock) => {
    const acked = acks.has(block.id);
    const chip = marginStateChip(block, acked);
    const isWarn = blockNeedsReview(block);
    // #113 deliberately clears a warning acknowledgement after an edit. An edited
    // visible warning must therefore remain acknowledgeable, not become a dead end.
    const showAck = isWarn && !acked;

    return (
      <div className="dmargin noprint">
        <span className={`chip ${chip.className}`}>{t(chip.key as ChromeKey)}</span>
        {block.note && <span className="mnote">{block.note}</span>}
        {block.provenance && <span className="mnote">{t('review.source')}{block.provenance.source || '—'} · {t('review.generator')}{block.provenance.generator || '—'}</span>}
        {block.provenance && <span className="mnote">{t('review.checks')}{block.provenance.gates?.length ? block.provenance.gates.join(', ') : '—'}</span>}
        {block.provenance?.external_options && <span className="mnote">{t('review.external')}</span>}
        <span className="mtools">
          <button type="button" title={t('review.edit')} data-action="b-edit" onClick={() => setEditingBlockId(block.id)} disabled={loading}>✎</button>
          <button type="button" title={t('review.up')} data-action="b-up" onClick={() => onMoveBlock(block.id, 'up')} disabled={loading}>↑</button>
          <button type="button" title={t('review.down')} data-action="b-down" onClick={() => onMoveBlock(block.id, 'down')} disabled={loading}>↓</button>
          <button type="button" title={t('review.remove')} data-action="b-del" onClick={() => onRemoveBlock(block.id)} disabled={loading}>✕</button>
          {showAck && (
            <button type="button" className="accept ack-btn" data-action="b-accept" onClick={() => onAckWarning(block.id)} disabled={loading}>
              {t('review.ack')}
            </button>
          )}
        </span>
      </div>
    );
  };

  const renderPlannedBlock = (block: ReviewBlock) => (
    <div
      key={block.id}
      id={`blk${block.id}`}
      className={`dblock block ${block.mark === 'warn' ? 'warn' : 'ok'} ${block.edited ? 'edited' : ''} ${editingBlockId === block.id ? 'editing' : ''}`}
      data-block-id={block.id}
    >
      <span className="type">
        {t(activityTypeLabel(block.type) as ChromeKey)}
        {block.mode && <span className="chip muted mode-chip">{block.mode}</span>}
      </span>
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

      {showFocusNotice && (
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
        <span className="duration-picker">
          <span className="duration-label">{t('review.durationLabel')}</span>
          {([45, 60, 90] as LessonDuration[]).map((v) => (
            <button
              key={v}
              type="button"
              className={`choice ${v === lesson.duration ? 'on' : ''}`}
              data-action="review-duration"
              data-v={v}
              onClick={() => onDurationChange(v)}
              disabled={loading || v === lesson.duration}
            >
              {t('review.durationChip', { minutes: v })}
            </button>
          ))}
          <span className="duration-hint">{t('review.durationHint')}</span>
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
          const phaseData = byPhase[phase];
          const label = PHASE_LABELS[phase];
          return (
            <section key={phase} className="review-phase" data-phase={phase}>
              <h4 className="dochead">
                <span className="pn">{t(label.pnKey)}</span>
                {t(label.titleKey)}
                <span className="pd">{t('review.phaseDuration', { minutes: label.pd[lesson.duration] })}</span>
              </h4>
              {phaseData.visible.length === 0 && (
                <div className="dblock block empty-phase">
                  <p>{t('review.emptyPhase')}</p>
                </div>
              )}
              {phaseData.visible.map(renderPlannedBlock)}
            </section>
          );
        })}
        <p className="docsheet-footer" data-testid="honesty-footer">
          {t('print.honestyFooter')}
        </p>
        {/* The banner is screen chrome and prints away with the rest of it. The
            caveat itself must survive onto paper, so it rides the sheet too. */}
        {showFocusNotice && (
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
          <p className="reserve-head">
            <b>{t('review.reserveHead', { duration: lesson.duration })}</b>
          </p>
          {reserve.map((block) => (
            <div key={block.id} className="dblock block reserve-block" data-block-id={block.id}>
              <span className="type">
                {t(activityTypeLabel(block.type) as ChromeKey)}
                {block.mode && <span className="chip muted mode-chip">{block.mode}</span>}
              </span>
              <div className="bcontent" data-activity-type={block.type}>
                <ActivityPlayer activity={block.activity} isUkrainian />
              </div>
              <div className="dmargin noprint">
                <span className="mtools">
                  <button type="button" data-action="b-include" onClick={() => onIncludeReserve(block.id)} disabled={loading}>
                    {t('review.include')}
                  </button>
                </span>
              </div>
            </div>
          ))}
        </section>
      )}

      {lesson.rejected.length > 0 && (
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
