import React, { useState, useEffect } from 'react';
import { ActivityPlayer } from '@learn-ukrainian/activity-kit';
import './conductor.css';

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
}

// Durations and phase budgets — faithful to demo
const DUR: Record<number, { tasks: number; pd: readonly [number, number, number] }> = {
  45: { tasks: 6, pd: [10, 15, 15] },
  60: { tasks: 9, pd: [15, 25, 15] },
  90: { tasks: 12, pd: [20, 40, 25] },
};

function durInfo(d: number) {
  return DUR[d] || DUR[45];
}

function condFmt(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s < 10 ? '0' : ''}${s}`;
}

const PHASE_NAMES: Record<1 | 2 | 3, string> = {
  1: 'Тест 1',
  2: 'Навчання',
  3: 'Тест 2',
};

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

function getReceipt(b: ConductorLessonDoc['blocks'][number]) {
  const warn = b.mark === 'warn';
  const why: Record<number, string> = {
    1: 'Перевірити розуміння прочитаного (Тест 1)',
    2: 'Відпрацювати мовну ціль (Навчання)',
    3: 'Закріпити й перевірити ще раз (Тест 2)',
  };
  return {
    src: warn
      ? 'Складено навколо тексту вчителя; частину (варіанти чи означення) додала «Граматка» — не з тексту'
      : 'Складено з тексту вчителя',
    why: why[b.phase] || 'Частина заняття',
    conf: warn ? 'середня' : 'висока',
    rej: warn ? (b.note || 'позначено ⚠ на ваш перегляд') : null,
  };
}

const COND_PROFILE = [
  { k: 'Засвоїв', v: 'вищий ступінь із «ніж» (тепліший, ніж…)' },
  { k: 'Часта помилка', v: '«дешевіша» замість «дешевша»; «сама краща» замість «найкраща»' },
  { k: 'Вагається', v: 'найвищий ступінь (най-)' },
  { k: 'Успішно виправив', v: '«на поверху» → «на поверсі»' },
];

export default function Conductor({ lessonDoc, onExit }: ConductorProps) {
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
  }, [lessonDoc]);

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
        <h3>Немає активного заняття</h3>
        <p>Відкрийте прийняте заняття й натисніть «▶ Провести заняття».</p>
        <button className="btn primary" onClick={onExit}>До списку</button>
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
                {p === 1 ? '①' : p === 2 ? '②' : '③'} {PHASE_NAMES[phh]}
              </div>
              <div className="tm">
                <span className="el">{condFmt(usedP)}</span>
                <span>
                  {c.pd[phh - 1]} хв
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
          {ph === 1 ? '①' : ph === 2 ? '②' : '③'} {PHASE_NAMES[ph]}
        </span>
        <span className="big">{condFmt(used)}</span>
        <span>/ {c.pd[ph - 1]}:00 бюджет</span>
        {over && <span className="warnpill">+{condFmt(used - budgetSec)}</span>}
        {brk > 0 && (
          <span style={{ flex: 1 }} />
        )}
        {brk > 0 && <span>＋ {brk} хв перерва</span>}
      </div>
    );
  }

  function renderReb() {
    if (!showReb || !reserveId) return null;
    const rb = getBlockById(blocks, reserveId);
    return (
      <div id="cond-reb" className="cond-reb">
        ⏱ Відстаємо від часу.
        <span className="sp" />
        <button
          className="btn ghost cond-sm"
          onClick={() => reserveId && doShorten(reserveId)}
        >
          Скоротити план
        </button>
        {rb && <span style={{ fontSize: 12, opacity: 0.8 }}>(прибрати «{rb.type}»)</span>}
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
          🔎 Попередній прогін <span className="cond-newpill">приклад</span> ·{' '}
          <span style={{ color: 'var(--ok)' }}>{okB.length} готові</span> ·{' '}
          <span style={{ color: 'var(--warn)' }}>{cautionB.length} варто глянути</span>
          {held ? <span style={{ color: 'var(--bad)' }}> · {held} відкладено</span> : null}
          <span className="sp" />
          {c.pfOpen ? '▲' : '▼'}
        </div>
        {c.pfOpen && (
          <>
            <div className="cond-pfsub">
              приклад майбутньої функції: перед заняттям «Граматка» зможе прогнати його з кількома змодельованими учнями рівня B1 — щоб зловити хитрі місця заздалегідь. Тут показано, як це виглядатиме
            </div>
            <ul>
              {cautionB.length ? (
                cautionB.map((b, idx) => (
                  <li key={idx}>
                    <b>«{b.type}»</b> — {b.note || 'позначено на ваш перегляд'}
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
    if (!curB) return <div>Кінець плану.</div>;
    return (
      <div className="cond-shared">
        <div className="cond-taskmeta">
          <span className="num">Завдання {visIndex} з {total}</span>
        </div>
        <ActivityPlayer activity={curB.activity} isUkrainian />
      </div>
    );
  }

  function renderTeacherPanel() {
    if (!curB) return null;
    const markChip = curB.mark === 'warn' ? (
      <span className="chip warn">⚠ погляньте</span>
    ) : (
      <span className="chip ok">✓ перевірено</span>
    );
    const resTag = ''; // no explicit reserve tag in real data; shorten logic uses last-in-phase
    return (
      <div className="cond-panel">
        <div className="cond-panellabel">🔒 Ваша панель — учень цього не бачить</div>
        <div className="cond-panelchips">
          <span className="chip info">{curB.type}</span>
          <span className="chip muted">{curB.mode}</span>
          {resTag}
          {markChip}
        </div>
        {curB.note && <div className="cond-note">⚠ {curB.note}</div>}
        {c.showAns && curB.answer_key != null && (
          <div className="cond-answer">
            <span className="lbl">🔑 Відповідь</span>
            <div className="val">
              {typeof curB.answer_key === 'string' ? curB.answer_key : JSON.stringify(curB.answer_key, null, 2)}
            </div>
          </div>
        )}
        {c.showReceipt && (
          <div className="cond-receipt">
            <div className="rh">📎 ЧОМУ ЦЕ ЗАВДАННЯ ІСНУЄ</div>
            {(() => {
              const r = getReceipt(curB);
              return (
                <>
                  <div className="row"><span className="k">Джерело:</span><span>{r.src}</span></div>
                  <div className="row"><span className="k">Навіщо:</span><span>{r.why}</span></div>
                  <div className="row"><span className="k">Впевненість:</span><span className="conf">{r.conf}</span></div>
                  {r.rej && <div className="row"><span className="k">Позначено:</span><span className="rej">{r.rej}</span></div>}
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
        <div className="cond-sharedlabel">👩‍🎓 Спільний екран — це бачить учень</div>
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
          <button className="btn primary" onClick={toggleStud}>✏ Повернутися до панелі</button>
        </div>
      );
    }
    const b = curB;
    return (
      <div className="cond-ctl">
        <button className="btn ghost cond-sm" onClick={goPrev} disabled={c.i === 0}>
          ← Назад
        </button>
        <button className="btn ghost cond-sm" onClick={toggleAns}>
          {c.showAns ? '🔑 Сховати' : '🔑 Відповідь'}
        </button>
        <button
          className={`btn ghost cond-sm${c.showReceipt ? ' cond-on' : ''}`}
          onClick={toggleReceipt}
        >
          ⓘ Чому це завдання?
        </button>
        <span className="cond-grow" />
        <button
          className={`btn ghost cond-sm${b && c.easy[b.id] ? ' cond-on' : ''}`}
          onClick={toggleEasy}
        >
          🙂 Занадто легко
        </button>
        <button
          className={`btn ghost cond-sm${b && c.gate[b.id] ? ' cond-on' : ''}`}
          onClick={toggleGate}
        >
          ⚑ Перевірка помилилася
        </button>
        <button className="btn ghost cond-sm" onClick={() => goNextOrSkip(true)}>
          ⤼ Пропустити
        </button>
        <button className="btn primary cond-sm" onClick={() => goNextOrSkip(false)}>
          ✓ Готово
        </button>
      </div>
    );
  }

  function renderSummary() {
    const flags: string[] = [];
    blocks.forEach((b) => {
      if (c.gate[b.id]) flags.push(`⚑ «${b.type}» — перевірка помилилася (перевірити правило)`);
      if (c.easy[b.id]) flags.push(`🙂 «${b.type}» — занадто легко для цього учня`);
      if (c.skip[b.id]) flags.push(`⤼ «${b.type}» — пропущено на занятті`);
    });
    (c.dropped || []).forEach((t) => {
      flags.push(`✂ «${t}» — прибрано із плану через брак часу (у запас/на домашнє)`);
    });

    return (
      <div className="cond-sum">
        <h2>Що сталося на занятті</h2>
        <p className="cond-lead">
          Приклад: так виглядатиме запис, який «Граматка» зможе зберігати після кожного заняття (за згодою учня). Персональних даних учня зараз не зберігаємо.
        </p>

        <div className="cond-grid">
          <div className="cond-stat"><div className="n">{doneCount}</div><div className="l">виконано</div></div>
          <div className="cond-stat"><div className="n">{skipCount}</div><div className="l">пропущено</div></div>
          <div className="cond-stat"><div className="n">{easyCount}</div><div className="l">легкі</div></div>
          <div className="cond-stat"><div className="n">{gateCount}</div><div className="l">«перевірка помилилася»</div></div>
        </div>

        <h4 className="dochead"><span className="pn">⏱</span>Час за частинами</h4>
        <table className="cond-tbl">
          <thead><tr><th>Частина</th><th>план</th><th>факт</th></tr></thead>
          <tbody>
            {[1, 2, 3].map((p) => {
              const phh = p as 1 | 2 | 3;
              const a = c.elapsed[phh] || 0;
              const overP = a > c.pd[phh - 1] * 60;
              return (
                <tr key={p}>
                  <td>{PHASE_NAMES[phh]}</td>
                  <td>{c.pd[phh - 1]}:00</td>
                  <td className={overP ? 'cond-over' : ''}>{condFmt(a)}{overP ? ' ⚠' : ''}</td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <h4 className="dochead"><span className="pn">⚑</span>Позначки для «Граматки»</h4>
        {flags.length ? (
          <ul className="cond-flaglist">{flags.map((f, idx) => <li key={idx}>{f}</li>)}</ul>
        ) : (
          <p className="cond-lead">Позначок немає — усе пройшло гладко.</p>
        )}

        {c.ruleDone === 'acc' && (
          <div className="cond-rulecard">
            <div className="rt">✓ Приклад: так це правило запамʼяталося б у памʼятці про цього учня (у демо нічого не зберігається).</div>
          </div>
        )}
        {c.ruleDone !== 'rej' && c.ruleDone !== 'acc' && (
          <div className="cond-rulecard">
            <div className="rt">💡 Правило для «Граматки» (приклад майбутньої функції)</div>
            <div className="rq">«Для цього учня вводьте вищий ступінь прикметників контрастно (більший ↔ менший).»</div>
            <div className="cond-rulenote">Це нотатка про ЦЬОГО учня, а не загальне правило.</div>
            <div className="btns">
              <button className="btn primary cond-sm" onClick={() => acceptRule('acc')}>Прийняти правило</button>
              <button className="btn ghost cond-sm" onClick={() => acceptRule('rej')}>Не треба</button>
            </div>
          </div>
        )}

        <div className="cond-sumbtns">
          <button className="btn primary" onClick={restart}>↺ Провести ще раз</button>
          <button className="btn ghost" onClick={exitToLesson}>До готового заняття</button>
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
      <div className="conductor-view">
        <div className="rolebanner student">👩‍🎓 ЕКРАН УЧНЯ — без відповідей і підказок. Безпечно ділитися в Zoom.</div>
        <div className="cond-card">{renderShared()}</div>
        <div className="cond-ctl">{renderCtl()}</div>
      </div>
    );
  }

  const heldN = (l.rejected && l.rejected.length) || 0;
  const held = heldN ? (
    <div className="cond-held">🛑 <b>Відкладено на перевірку:</b> {heldN} завдання, що не пройшли перевірку, — до заняття не ввійшли (їх видно у «Перегляді»).</div>
  ) : null;

  const hint = c.hintOff ? null : (
    <div className="hintbar">
      <span>💡 Ви проводите вже готове й перевірене заняття, крок за кроком. Смужка вгорі — три частини й час на кожну. «ⓘ Чому це завдання?» показує, звідки воно взялося. «＋5 хв» пришвидшує годинник, щоб побачити, як план підлаштовується під час.</span>
      <button onClick={dismissHint} title="сховати підказку">×</button>
    </div>
  );

  return (
    <div className="conductor-view">
      <div className="cond-top">
        <div className="cond-title">
          <b>▶ Проведення заняття</b>
          <span className="cond-sub">{l.title} · {c.dur} хв</span>
        </div>
        <div className="cond-topbtns">
          <button className="btn ghost cond-sm" onClick={exitToLesson}>← Вийти</button>
          <button className="btn ghost cond-sm" onClick={addTime}>＋5 хв ⏱</button>
          <button className="btn ghost cond-sm" onClick={() => setShowProfile(true)}>👤 Профіль учня</button>
          <button className="btn ghost cond-sm" onClick={toggleStud}>👁 Як бачить учень</button>
          <button className="btn ghost cond-sm" onClick={() => setShowHelp(true)}>? Довідка</button>
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
            <h2>👤 Профіль учня <span className="cond-newpill">МАЙБУТНЄ</span></h2>
            <p className="cond-modp">Памʼять про учня, зібрана з його реальних відповідей — щоб наступні заняття були під нього, а не просто «рівня B1». Зʼявляється лише після реєстрації учня, за згодою.</p>
            {COND_PROFILE.map((it, idx) => (
              <div className="cond-lrow" key={idx}>
                <span className="k">{it.k}:</span>
                <span>{it.v}</span>
              </div>
            ))}
            <div className="cond-optin">🔒 Ця памʼять зʼявиться, коли учень зареєструється — за його згодою. Поки що її бачите тільки ви.</div>
            <div style={{ marginTop: 14, textAlign: 'right' }}>
              <button className="btn primary" onClick={() => setShowProfile(false)}>Закрити</button>
            </div>
          </div>
        </div>
      )}

      {/* Help modal */}
      {showHelp && (
        <div className="cond-overlay" onClick={(e) => { if (e.target === e.currentTarget) setShowHelp(false); }}>
          <div className="cond-modal">
            <button className="cond-close" onClick={() => setShowHelp(false)}>×</button>
            <h2>Довідка — режим «Проведення заняття»</h2>
            <h3 className="cond-modh">Що це?</h3>
            <p className="cond-modp">«Проведення заняття» — режим, у якому ви <b>проводите вже готове й перевірене заняття</b> просто на екрані під час уроку (наприклад, у Zoom). Завдання показуються по одному. «Граматка» не звертається до штучного інтелекту наживо — заняття вже складене й заморожене.</p>
            <h3 className="cond-modh">Спільний екран</h3>
            <p className="cond-modp">Центральний блок із зеленою рамкою — це те саме, що бачить учень: інтерактивне завдання без відповідей. У Zoom є три способи: ви натискаєте самі (учень відповідає усно); або ділитеся <b>чистим екраном учня</b> — кнопка «👁 Як бачить учень» ховає вашу панель; або даєте учневі посилання, щоб натискав він сам.</p>
            <h3 className="cond-modh">Ваша панель</h3>
            <p className="cond-modp">Жовта панель під завданням — приватна, учень її не бачить: 🔑 відповідь, позначка перевірки, ⓘ «чому це завдання», і кнопки керування.</p>
            <h3 className="cond-modh">Смужка часу вгорі</h3>
            <p className="cond-modp">Три частини заняття (Тест → Навчання → Тест) і час на кожну. Годинник іде для поточної частини; якщо перевищуєте бюджет — він стає жовтим і зʼявляється підказка «скоротити».</p>
            <h3 className="cond-modh">Кнопки під завданням</h3>
            <p className="cond-modp"><b>Готово</b> — далі · <b>Пропустити</b> · <b>Занадто легко</b> — для цього учня · <b>Перевірка помилилася</b> — коли перевірка дарма щось позначила · <b>🔑</b> — відповідь бачите тільки ви · <b>ⓘ Чому це завдання?</b> — звідки воно взялося.</p>
            <h3 className="cond-modh">🔎 Попередній прогін · 🛑 Відкладено</h3>
            <p className="cond-modp"><i>Приклад майбутньої функції:</i> перед заняттям система зможе «прогнати» його з кількома змодельованими учнями рівня B1, щоб зловити хитрі місця заздалегідь. А що не пройшло перевірку — не потрапляє в заняття, а чекає на ваш перегляд. Нічого не зникає тихо.</p>
            <div style={{ marginTop: 16, textAlign: 'right' }}>
              <button className="btn primary" onClick={() => setShowHelp(false)}>Закрити</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
