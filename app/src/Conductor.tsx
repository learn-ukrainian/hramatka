import React, { useState, useEffect } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import './conductor.css';
import { DeliberateErrorBadge } from './DeliberateError';
import { useT, type TFn, type ChromeKey } from './i18n';
import { deliberateErrors, formatAnswerKeyDisplay } from './review-helpers';

// Minimal shape for the lesson document (from app contract; no new API)
export interface ConductorLessonDoc {
  id: string;
  title: string;
  duration: 45 | 60 | 90 | number;
  blocks: Array<{
    id: string;
    phase: 1 | 2 | 3;
    type: string;
    mode: string;
    activity: any;
    answer_key: any;
    mark: 'ok' | 'warn';
    note: string | null;
  }>;
  rejected?: any[];
}

export interface ConductorProps {
  lessonDoc: ConductorLessonDoc;
  onExit: () => void;
  onStudentPreviewChange?: (preview: boolean) => void;
}

// Timing remains a UI concern; visible block counts come from the canonical
// review sizing policy rather than a second local task-budget map.
const DUR: Record<number, { pd: readonly [number, number, number] }> = {
  45: { pd: [10, 15, 15] },
  60: { pd: [15, 25, 15] },
  90: { pd: [20, 40, 25] },
};

function durInfo(d: number) {
  return DUR[d] || DUR[45];
}

function condFmt(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

/** Phase label key (chrome from demo CONDT ph1/ph2/ph3). */
function phaseKey(ph: 1 | 2 | 3): ChromeKey {
  return ph === 1 ? 'cond.ph1' : ph === 2 ? 'cond.ph2' : 'cond.ph3';
}

interface CondState {
  lid: string;
  dur: number;
  pd: readonly [number, number, number];
  i: number; // index in plan
  plan: string[];
  done: Record<string, boolean>;
  skip: Record<string, boolean>;
  easy: Record<string, boolean>;
  gate: Record<string, boolean>;
  showAns: boolean;
  showReceipt: boolean;
  stud: boolean;
  dropped: string[];
  elapsed: Record<1 | 2 | 3, number>;
  started: boolean;
  ended: boolean;
  ruleDone: 'acc' | 'rej' | null;
  pfOpen: boolean;
  hintOff: number;
}

function initCond(l: ConductorLessonDoc): CondState {
  const d = (l.duration || 45) as number;
  const info = durInfo(d);
  const pd = info.pd;
  // Honest adaptation: use the blocks the lesson already contains (engine chose the set for this duration).
  // Demo sliced using planSplit; real lessons already embody the selected set.
  const plan = (l.blocks || []).map((b) => b.id);
  return {
    lid: l.id,
    dur: d,
    pd,
    i: 0,
    plan,
    done: {},
    skip: {},
    easy: {},
    gate: {},
    showAns: false,
    showReceipt: false,
    stud: false,
    dropped: [],
    elapsed: { 1: 0, 2: 0, 3: 0 },
    started: true,
    ended: false,
    ruleDone: null,
    pfOpen: false,
    hintOff: 0,
  };
}

function getBlockById(blocks: ConductorLessonDoc['blocks'], id: string) {
  return blocks.find((b) => b.id === id) || null;
}

function getCurBlock(cond: CondState, blocks: ConductorLessonDoc['blocks']) {
  return getBlockById(blocks, cond.plan[cond.i]) || null;
}

function getCurPhase(cond: CondState, blocks: ConductorLessonDoc['blocks']): 1 | 2 | 3 {
  const b = getCurBlock(cond, blocks);
  return (b?.phase || 3) as 1 | 2 | 3;
}

// Adaptation: last remaining block in the phase (ahead or at i) acts as the "reserve" droppable.
// This matches demo intent (last/highest in phase slice) without demo-only seed ids.
function getReserveCandidate(cond: CondState, blocks: ConductorLessonDoc['blocks'], phase: number): string | null {
  const candidates: Array<{ id: string; idx: number }> = [];
  cond.plan.forEach((id, idx) => {
    if (idx >= cond.i) {
      const b = getBlockById(blocks, id);
      if (b && b.phase === phase) {
        candidates.push({ id, idx });
      }
    }
  });
  if (candidates.length === 0) return null;
  // last in phase remaining = droppable when over budget
  return candidates[candidates.length - 1].id;
}

function getReceipt(b: ConductorLessonDoc['blocks'][number], t: TFn) {
  const warn = b.mark === 'warn';
  const whyKey: Record<number, ChromeKey> = {
    1: 'cond.rc.why1',
    2: 'cond.rc.why2',
    3: 'cond.rc.why3',
  };
  return {
    src: warn ? t('cond.rc.srcWarn') : t('cond.rc.srcOk'),
    why: t(whyKey[b.phase] ?? 'cond.rc.whyDefault'),
    conf: warn ? t('cond.rc.confMedium') : t('cond.rc.confHigh'),
    rej: warn ? (b.note || t('cond.rc.rejDefault')) : null, // b.note = content, stays UA
  };
}

// Future-labelled stub content (demo COND_PROFILE): chrome via dictionary keys.
const COND_PROFILE_KEYS: Array<{ k: ChromeKey; v: ChromeKey }> = [
  { k: 'cond.prof1.k', v: 'cond.prof1.v' },
  { k: 'cond.prof2.k', v: 'cond.prof2.v' },
  { k: 'cond.prof3.k', v: 'cond.prof3.v' },
  { k: 'cond.prof4.k', v: 'cond.prof4.v' },
];

export default function Conductor({ lessonDoc, onExit, onStudentPreviewChange }: ConductorProps) {
  const { t } = useT();
  const phaseName = (ph: 1 | 2 | 3) => t(phaseKey(ph));
  const [cond, setCond] = useState<CondState | null>(null);
  const [showProfile, setShowProfile] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  // blocks reference (stable for this run)
  const blocks = lessonDoc.blocks || [];

  // Initialize on mount / lesson change
  useEffect(() => {
    const c = initCond(lessonDoc);
    setCond(c);
    setShowProfile(false);
    setShowHelp(false);
    onStudentPreviewChange?.(false);
  }, [lessonDoc, onStudentPreviewChange]);

  useEffect(() => {
    onStudentPreviewChange?.(!!cond?.stud);
  }, [cond?.stud, onStudentPreviewChange]);

  useEffect(() => {
    return () => { onStudentPreviewChange?.(false); };
  }, [onStudentPreviewChange]);

  // Live per-second clock — only current phase, only while running
  useEffect(() => {
    if (!cond || cond.ended || !cond.started) return undefined;
    const timer = setInterval(() => {
      setCond((prev) => {
        if (!prev || prev.ended) return prev;
        const ph = getCurPhase(prev, blocks) as 1 | 2 | 3;
        const nextElapsed = { ...prev.elapsed, [ph]: (prev.elapsed[ph] || 0) + 1 };
        return { ...prev, elapsed: nextElapsed };
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [cond?.started, cond?.ended, cond?.i, blocks]); // i changes reset phase context

  if (!cond) {
    return (
      <div className="emptystate">
        <h3>{t('cond.empty.title')}</h3>
        <p>{t('cond.empty.body')}</p>
        <button className="btn primary" onClick={onExit}>{t('cond.empty.toList')}</button>
      </div>
    );
  }

  // non-null after guard for render (TS control flow for nested fns)
  const c = cond!;

  const l = lessonDoc;
  const curB = getCurBlock(c, blocks);
  const ph = getCurPhase(c, blocks);
  const used = c.elapsed[ph] || 0;
  const budgetSec = c.pd[ph - 1] * 60;
  const over = used > budgetSec;
  const total = c.plan.length;
  const visIndex = c.i + 1;

  // Derived counts for UI
  const doneCount = Object.keys(c.done).length;
  const skipCount = Object.keys(c.skip).length;
  const easyCount = Object.keys(c.easy).filter((k) => c.easy[k]).length;
  const gateCount = Object.keys(c.gate).filter((k) => c.gate[k]).length;

  // Over budget shorten candidate (last remaining in phase)
  const reserveId = getReserveCandidate(c, blocks, ph);
  const showReb = !c.stud && over && !!reserveId;

  function doShorten(id: string) {
    const b = getBlockById(blocks, id);
    setCond((prev) => {
      if (!prev) return prev;
      const dropped = b ? [...prev.dropped, b.type] : prev.dropped;
      const newPlan = prev.plan.filter((x) => x !== id);
      // If we removed current or before, adjust i conservatively
      let newI = prev.i;
      if (newPlan.length <= newI) newI = Math.max(0, newPlan.length - 1);
      return { ...prev, dropped, plan: newPlan, i: Math.min(newI, newPlan.length - 1) };
    });
  }

  function goPrev() {
    setCond((prev) => {
      if (!prev || prev.i <= 0) return prev;
      return { ...prev, i: prev.i - 1, showAns: false, showReceipt: false };
    });
  }

  function goNextOrSkip(isSkip: boolean) {
    setCond((prev) => {
      if (!prev) return prev;
      const cb = getBlockById(blocks, prev.plan[prev.i]);
      if (!cb) return prev;
      const nextDone = isSkip ? prev.done : { ...prev.done, [cb.id]: true };
      const nextSkip = isSkip ? { ...prev.skip, [cb.id]: true } : prev.skip;
      const nextI = prev.i + 1;
      const ended = nextI >= prev.plan.length;
      return {
        ...prev,
        done: nextDone,
        skip: nextSkip,
        i: Math.min(nextI, prev.plan.length - 1),
        showAns: false,
        showReceipt: false,
        ended,
      };
    });
  }

  function toggleAns() {
    setCond((prev) => (prev ? { ...prev, showAns: !prev.showAns } : prev));
  }

  function toggleReceipt() {
    setCond((prev) => (prev ? { ...prev, showReceipt: !prev.showReceipt } : prev));
  }

  function toggleEasy() {
    setCond((prev) => {
      if (!prev || !curB) return prev;
      return { ...prev, easy: { ...prev.easy, [curB.id]: !prev.easy[curB.id] } };
    });
  }

  function toggleGate() {
    setCond((prev) => {
      if (!prev || !curB) return prev;
      return { ...prev, gate: { ...prev.gate, [curB.id]: !prev.gate[curB.id] } };
    });
  }

  function toggleStud() {
    setCond((prev) => {
      if (!prev) return prev;
      const nextStud = !prev.stud;
      return { ...prev, stud: nextStud, showAns: nextStud ? false : prev.showAns };
    });
  }

  function addTime() {
    setCond((prev) => {
      if (!prev || prev.ended) return prev;
      const phh = getCurPhase(prev, blocks) as 1 | 2 | 3;
      return { ...prev, elapsed: { ...prev.elapsed, [phh]: (prev.elapsed[phh] || 0) + 300 } };
    });
  }

  function togglePf() {
    setCond((prev) => (prev ? { ...prev, pfOpen: !prev.pfOpen } : prev));
  }

  function dismissHint() {
    setCond((prev) => (prev ? { ...prev, hintOff: 1 } : prev));
  }

  function acceptRule(v: 'acc' | 'rej') {
    setCond((prev) => (prev ? { ...prev, ruleDone: v } : prev));
  }

  function restart() {
    const fresh = initCond(lessonDoc);
    setCond(fresh);
    setShowProfile(false);
    setShowHelp(false);
  }

  function exitToLesson() {
    onExit();
  }

  // Render helpers (port of cond*HTML)
  function renderRail() {
    const cp = ph;
    return (
      <div className="cond-rail" id="cond-rail">
        {[1, 2, 3].map((p) => {
          const phh = p as 1 | 2 | 3;
          const usedP = c.elapsed[phh] || 0;
          const budP = c.pd[phh - 1] * 60;
          const overP = usedP > budP;
          const pct = Math.min(100, (usedP / budP) * 100);
          const cls = [
            'cond-seg',
            p === cp ? 'active' : '',
            p < cp ? 'done' : '',
            overP ? 'over' : '',
          ]
            .filter(Boolean)
            .join(' ');
          return (
            <div className={cls} key={p}>
              <div className="nm">
                {p === 1 ? '①' : p === 2 ? '②' : '③'} {phaseName(phh)}
              </div>
              <div className="tm">
                <span className="el">{condFmt(usedP)}</span>
                <span>
                  {c.pd[phh - 1]} {t('cond.min')}
                </span>
              </div>
              <div className="bar">
                <div className="fill" style={{ width: `${pct}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  function renderClock() {
    const brk = c.dur - (c.pd[0] + c.pd[1] + c.pd[2]);
    return (
      <div className="cond-clock" id="cond-clock">
        <span>
          {ph === 1 ? '①' : ph === 2 ? '②' : '③'} {phaseName(ph)}
        </span>
        <span className="big">{condFmt(used)}</span>
        <span>/ {c.pd[ph - 1]}:00 {t('cond.budget')}</span>
        {over && <span className="warnpill">+{condFmt(used - budgetSec)}</span>}
        {brk > 0 && (
          <span style={{ flex: 1 }} />
        )}
        {brk > 0 && <span>＋ {brk} {t('cond.min')} {t('cond.break')}</span>}
      </div>
    );
  }

  function renderReb() {
    if (!showReb || !reserveId) return null;
    const rb = getBlockById(blocks, reserveId);
    return (
      <div id="cond-reb" className="cond-reb">
        {t('cond.behind')}
        <span className="sp" />
        <button
          className="btn ghost cond-sm"
          onClick={() => reserveId && doShorten(reserveId)}
        >
          {t('cond.shorten')}
        </button>
        {rb && <span style={{ fontSize: 12, opacity: 0.8 }}>{t('cond.removeType', { type: rb.type })}</span>}
      </div>
    );
  }

  function renderPreflight() {
    const vis = c.plan.map((id) => getBlockById(blocks, id)).filter(Boolean) as any[];
    const okB = vis.filter((b) => b.mark !== 'warn');
    const cautionB = vis.filter((b) => b.mark === 'warn');
    const held = (l.rejected && l.rejected.length) || 0;
    return (
      <div className="cond-pf">
        <div className="ph" onClick={togglePf}>
          {t('cond.pfTitle')} <span className="cond-newpill">{t('cond.example')}</span> ·{' '}
          <span style={{ color: 'var(--ok)' }}>{t('cond.pfReady', { n: okB.length })}</span> ·{' '}
          <span style={{ color: 'var(--warn)' }}>{t('cond.pfFlag', { n: cautionB.length })}</span>
          {held ? <span style={{ color: 'var(--bad)' }}> · {t('cond.pfHeld', { n: held })}</span> : null}
          <span className="sp" />
          {c.pfOpen ? '▲' : '▼'}
        </div>
        {c.pfOpen && (
          <>
            <div className="cond-pfsub">
              {t('cond.pfSub')}
            </div>
            <ul>
              {cautionB.length ? (
                cautionB.map((b, idx) => (
                  <li key={idx}>
                    <b>«{b.type}»</b> — {b.note || t('cond.pfItemDefault')}
                  </li>
                ))
              ) : (
                <li>—</li>
              )}
            </ul>
          </>
        )}
      </div>
    );
  }

  function renderShared() {
    if (!curB) return <div>{t('cond.endOfPlan')}</div>;
    return (
      <div className="cond-shared">
        <div className="cond-taskmeta">
          <span className="num">{t('cond.taskCount', { i: visIndex, total })}</span>
        </div>
        <div data-activity data-activity-type={curB.type}>
          <ActivityPlayer activity={curB.activity} isUkrainian />
        </div>
      </div>
    );
  }

  function renderTeacherPanel() {
    if (!curB) return null;
    const markChip = curB.mark === 'warn' ? (
      <span className="chip warn">{t('cond.markLook')}</span>
    ) : (
      <span className="chip ok">{t('cond.markVerified')}</span>
    );
    const resTag = ''; // no explicit reserve tag in real data; shorten logic uses last-in-phase
    // #208 / #164: teacher-only conduct panel. Badge answers "is this a bug?" so it
    // stays visible even with answers hidden. Never put this in renderShared() —
    // student preview reuses that surface and must not receive intent in the DOM.
    const intent = deliberateErrors(curB.answer_key);
    return (
      <div className="cond-panel">
        <div className="cond-panellabel">{t('cond.panelLabel')}</div>
        <div className="cond-panelchips">
          <span className="chip info">{curB.type}</span>
          <span className="chip muted">{curB.mode}</span>
          {resTag}
          {markChip}
        </div>
        {intent.length > 0 && <DeliberateErrorBadge />}
        {curB.note && <div className="cond-note">⚠ {curB.note}</div>}
        {c.showAns && curB.answer_key != null && (
          <div className="cond-answer" data-testid="cond-answer-key">
            <span className="lbl">{t('cond.answerLabel')}</span>
            <div className="val" style={{ whiteSpace: 'pre-wrap' }}>
              {formatAnswerKeyDisplay(curB.answer_key, curB.activity, t)}
            </div>
          </div>
        )}
        {c.showReceipt && (
          <div className="cond-receipt">
            <div className="rh">{t('cond.whyHead')}</div>
            {(() => {
              const r = getReceipt(curB, t);
              return (
                <>
                  <div className="row"><span className="k">{t('cond.rSrc')}</span><span>{r.src}</span></div>
                  <div className="row"><span className="k">{t('cond.rWhy')}</span><span>{r.why}</span></div>
                  <div className="row"><span className="k">{t('cond.rConf')}</span><span className="conf">{r.conf}</span></div>
                  {r.rej && <div className="row"><span className="k">{t('cond.rFlagged')}</span><span className="rej">{r.rej}</span></div>}
                </>
              );
            })()}
          </div>
        )}
      </div>
    );
  }

  function renderCard() {
    const shared = (
      <>
        <div className="cond-sharedlabel">{t('cond.sharedLabel')}</div>
        {renderShared()}
      </>
    );
    if (c.stud) {
      return <div className="cond-card">{shared}</div>;
    }
    return (
      <div className="cond-card">
        {shared}
        {renderTeacherPanel()}
      </div>
    );
  }

  function renderCtl() {
    if (c.stud) {
      return (
        <div className="cond-ctl">
          <button className="btn primary" data-testid="teacher-return-btn" onClick={toggleStud}>{t('cond.backToPanel')}</button>
        </div>
      );
    }
    const b = curB;
    return (
      <div className="cond-ctl">
        <button className="btn ghost cond-sm" onClick={goPrev} disabled={c.i === 0}>
          {t('cond.back')}
        </button>
        <button className="btn ghost cond-sm" onClick={toggleAns}>
          {c.showAns ? t('cond.hideAnsBtn') : t('cond.showAnsBtn')}
        </button>
        <button
          className={`btn ghost cond-sm${c.showReceipt ? ' cond-on' : ''}`}
          onClick={toggleReceipt}
        >
          {t('cond.whyBtn')}
        </button>
        <span className="cond-grow" />
        <button
          className={`btn ghost cond-sm${b && c.easy[b.id] ? ' cond-on' : ''}`}
          onClick={toggleEasy}
        >
          {t('cond.tooEasy')}
        </button>
        <button
          className={`btn ghost cond-sm${b && c.gate[b.id] ? ' cond-on' : ''}`}
          onClick={toggleGate}
        >
          {t('cond.checkWrong')}
        </button>
        <button className="btn ghost cond-sm" onClick={() => goNextOrSkip(true)}>
          {t('cond.skip')}
        </button>
        <button className="btn primary cond-sm" onClick={() => goNextOrSkip(false)}>
          {t('cond.done')}
        </button>
      </div>
    );
  }

  function renderSummary() {
    const flags: string[] = [];
    blocks.forEach((b) => {
      if (c.gate[b.id]) flags.push(t('cond.flag.gate', { type: b.type }));
      if (c.easy[b.id]) flags.push(t('cond.flag.easy', { type: b.type }));
      if (c.skip[b.id]) flags.push(t('cond.flag.skip', { type: b.type }));
    });
    (c.dropped || []).forEach((dt) => {
      flags.push(t('cond.flag.dropped', { type: dt }));
    });

    return (
      <div className="cond-sum">
        <h2>{t('cond.summary.title')}</h2>
        <p className="cond-lead">
          {t('cond.summary.lead')}
        </p>

        <div className="cond-grid">
          <div className="cond-stat"><div className="n">{doneCount}</div><div className="l">{t('cond.sDone')}</div></div>
          <div className="cond-stat"><div className="n">{skipCount}</div><div className="l">{t('cond.sSkip')}</div></div>
          <div className="cond-stat"><div className="n">{easyCount}</div><div className="l">{t('cond.sEasy')}</div></div>
          <div className="cond-stat"><div className="n">{gateCount}</div><div className="l">{t('cond.sFlag')}</div></div>
        </div>

        <h4 className="dochead"><span className="pn">⏱</span>{t('cond.phaseTime')}</h4>
        <table className="cond-tbl">
          <thead><tr><th>{t('cond.tblPhase')}</th><th>{t('cond.tblPlan')}</th><th>{t('cond.tblActual')}</th></tr></thead>
          <tbody>
            {[1, 2, 3].map((p) => {
              const phh = p as 1 | 2 | 3;
              const a = c.elapsed[phh] || 0;
              const overP = a > c.pd[phh - 1] * 60;
              return (
                <tr key={p}>
                  <td>{phaseName(phh)}</td>
                  <td>{c.pd[phh - 1]}:00</td>
                  <td className={overP ? 'cond-over' : ''}>{condFmt(a)}{overP ? ' ⚠' : ''}</td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <h4 className="dochead"><span className="pn">⚑</span>{t('cond.flagsHead')}</h4>
        {flags.length ? (
          <ul className="cond-flaglist">{flags.map((f, idx) => <li key={idx}>{f}</li>)}</ul>
        ) : (
          <p className="cond-lead">{t('cond.noFlags')}</p>
        )}

        {c.ruleDone === 'acc' && (
          <div className="cond-rulecard">
            <div className="rt">{t('cond.ruleOk')}</div>
          </div>
        )}
        {c.ruleDone !== 'rej' && c.ruleDone !== 'acc' && (
          <div className="cond-rulecard">
            <div className="rt">{t('cond.ruleTitle')}</div>
            <div className="rq">{t('cond.ruleText')}</div>
            <div className="cond-rulenote">{t('cond.ruleNote')}</div>
            <div className="btns">
              <button className="btn primary cond-sm" onClick={() => acceptRule('acc')}>{t('cond.ruleAcc')}</button>
              <button className="btn ghost cond-sm" onClick={() => acceptRule('rej')}>{t('cond.ruleRej')}</button>
            </div>
          </div>
        )}

        <div className="cond-sumbtns">
          <button className="btn primary" onClick={restart}>{t('cond.again')}</button>
          <button className="btn ghost" onClick={exitToLesson}>{t('cond.toReady')}</button>
        </div>
      </div>
    );
  }

  // Main view
  if (c.ended) {
    return <div className="conductor-view">{renderSummary()}</div>;
  }

  // Student preview is clean (banner + shared only + back control)
  if (c.stud) {
    return (
      <div className="conductor-view" data-testid="conductor-student-preview">
        <div className="rolebanner student" data-testid="student-banner">{t('cond.studBanner')}</div>
        <div className="cond-card">{renderShared()}</div>
        <div className="cond-ctl">{renderCtl()}</div>
      </div>
    );
  }

  const heldN = (l.rejected && l.rejected.length) || 0;
  const held = heldN ? (
    <div className="cond-held">🛑 <b>{t('cond.heldLabel')}</b> {t('cond.heldRest', { n: heldN })}</div>
  ) : null;

  const hint = c.hintOff ? null : (
    <div className="hintbar">
      <span>{t('cond.hint')}</span>
      <button onClick={dismissHint} title={t('cond.hideHint')}>×</button>
    </div>
  );

  return (
    <div className="conductor-view">
      <div className="cond-top">
        <div className="cond-title">
          <b>{t('cond.top.title')}</b>
          <span className="cond-sub">{l.title} · {c.dur} {t('cond.min')}</span>
        </div>
        <div className="cond-topbtns">
          <button className="btn ghost cond-sm" onClick={exitToLesson}>{t('cond.exit')}</button>
          <button className="btn ghost cond-sm" onClick={addTime}>{t('cond.addTime')}</button>
          <button className="btn ghost cond-sm" onClick={() => setShowProfile(true)}>{t('cond.profileBtn')}</button>
          <button className="btn ghost cond-sm" data-testid="enter-student-preview-btn" onClick={toggleStud}>{t('cond.studentView')}</button>
          <button className="btn ghost cond-sm" onClick={() => setShowHelp(true)}>{t('cond.helpBtn')}</button>
        </div>
      </div>

      {renderRail()}
      {renderClock()}
      {renderPreflight()}
      {hint}
      {renderReb()}
      {renderCard()}
      {renderCtl()}
      {held}

      {/* Profile modal */}
      {showProfile && (
        <div className="cond-overlay" onClick={(e) => { if (e.target === e.currentTarget) setShowProfile(false); }}>
          <div className="cond-modal">
            <button className="cond-close" onClick={() => setShowProfile(false)}>×</button>
            <h2>{t('cond.profileBtn')} <span className="cond-newpill">{t('cond.future')}</span></h2>
            <p className="cond-modp">{t('cond.profLead')}</p>
            {COND_PROFILE_KEYS.map((it, idx) => (
              <div className="cond-lrow" key={idx}>
                <span className="k">{t(it.k)}:</span>
                <span>{t(it.v)}</span>
              </div>
            ))}
            <div className="cond-optin">{t('cond.ledgerNote')}</div>
            <div style={{ marginTop: 14, textAlign: 'right' }}>
              <button className="btn primary" onClick={() => setShowProfile(false)}>{t('cond.close')}</button>
            </div>
          </div>
        </div>
      )}

      {/* Help modal. Paragraphs use dangerouslySetInnerHTML to keep the demo's inline
          <b>/<i> emphasis. Safe: the HTML is a static dictionary constant (no user input,
          no interpolated params) — same trust model as the demo's chrome HTML. */}
      {showHelp && (
        <div className="cond-overlay" onClick={(e) => { if (e.target === e.currentTarget) setShowHelp(false); }}>
          <div className="cond-modal">
            <button className="cond-close" onClick={() => setShowHelp(false)}>×</button>
            <h2>{t('cond.help.title')}</h2>
            <h3 className="cond-modh">{t('cond.help.q1')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a1') }} />
            <h3 className="cond-modh">{t('cond.help.q2')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a2') }} />
            <h3 className="cond-modh">{t('cond.help.q3')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a3') }} />
            <h3 className="cond-modh">{t('cond.help.q4')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a4') }} />
            <h3 className="cond-modh">{t('cond.help.q5')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a5') }} />
            <h3 className="cond-modh">{t('cond.help.q6')}</h3>
            <p className="cond-modp" dangerouslySetInnerHTML={{ __html: t('cond.help.a6') }} />
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <button className="btn primary" onClick={() => setShowHelp(false)}>{t('cond.close')}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
