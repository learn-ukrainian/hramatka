/**
 * Dual-language (uk/en) chrome translation layer.
 *
 * Ports the BEHAVIOUR of the teacher-approved demo (hramatka/design/demo-reference.html)
 * — its T_EXACT / CHROME_EN / CONDT dictionaries and header EN/УКР toggle — as a proper
 * React key-based translation layer, NOT the demo's DOM-walker.
 *
 * Rules (user mandate, escalated 3×): EVERYTHING chrome is dual-language — header, views,
 * buttons, hints, status labels, error banners (client chrome), overlays, conductor
 * (including its FUTURE-labelled stubs). ONLY lesson CONTENT stays Ukrainian in both modes:
 * activities, anchor text, generated lesson strings, teacher-entered text, and any
 * server-provided message (those flow through as `raw`, never through this layer).
 *
 * Default language is Ukrainian. The `uk` value of every key is byte-identical to the
 * string that previously lived in the JSX, so existing UA tests are unaffected. EN wording
 * reuses the demo's own translations verbatim wherever the demo already has the string.
 */
import {
  createContext,
  useContext,
  useMemo,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from 'react';

export type Lang = 'uk' | 'en';

const LANG_KEY = 'hramatka:ui-lang';

export function loadLang(): Lang {
  try {
    return localStorage.getItem(LANG_KEY) === 'en' ? 'en' : 'uk';
  } catch {
    return 'uk';
  }
}

export function saveLang(lang: Lang): void {
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    /* private mode / quota — in-memory state still holds for the session */
  }
}

/** Session-boundary reset (#106): back to default UA + drop the persisted choice, like other client state. */
export function clearLang(): void {
  try {
    localStorage.removeItem(LANG_KEY);
  } catch {
    /* private mode / quota */
  }
}

type Entry = { uk: string; en: string };

/**
 * The chrome dictionary. `{name}` tokens are replaced from `t(key, params)`.
 * Comments cite the demo dictionary a value is sourced from (T_EXACT / CHROME_EN / CONDT).
 */
const DICT = {
  // ---- header / document ----
  brand: { uk: 'Граматка', en: 'Hramatka' },
  'doc.title': { uk: 'Граматка — Викладач (пілот)', en: 'Hramatka — Teacher (pilot)' },
  'lang.title': {
    uk: 'мова інтерфейсу (вміст занять — завжди українською)',
    en: 'interface language (lesson content is always Ukrainian)',
  }, // demo langbtn title
  'help.aria': { uk: 'Довідка', en: 'Help' }, // T_EXACT
  logout: { uk: 'Вийти', en: 'Sign out' },

  // ---- help overlay ----
  'help.title': { uk: 'Як користуватися «Граматкою»', en: 'How to use Hramatka' }, // T_EXACT
  'help.p1.b': { uk: 'Створення уроку.', en: 'Creating a lesson.' },
  'help.p1.t': {
    uk: 'Вставте український текст, оберіть тривалість і натисніть «Згенерувати урок». Рівень B1 фіксований для пілоту.',
    en: 'Paste a Ukrainian text, choose a duration and press “Generate lesson”. Level B1 is fixed for the pilot.',
  },
  'help.p2.b': { uk: 'Скільки чекати.', en: 'How long to wait.' },
  'help.p2.t': {
    uk: 'Генерація зазвичай триває кілька хвилин. Можна повернутися до списку — урок з’явиться, коли буде готовий.',
    en: 'Generation usually takes a few minutes. You can go back to the list — the lesson will appear when it is ready.',
  },
  'help.p3.b': { uk: '«Перевірте».', en: '“Verify.”' },
  'help.p3.t': {
    uk: 'Попередження означає, що завдання варто переглянути. Підтвердіть кожне перед прийняттям уроку.',
    en: 'A warning means the task is worth reviewing. Confirm each one before accepting the lesson.',
  },
  'help.p4.b': { uk: 'Якщо сталася помилка.', en: 'If an error occurs.' },
  'help.p4.t': {
    uk: 'Текст зберігається — натисніть «Створити урок ще раз із цим текстом» або поверніться до списку.',
    en: 'Your text is saved — press “Create the lesson again from this text” or go back to the list.',
  },
  'help.gotIt': { uk: 'Зрозуміло', en: 'Got it' }, // T_EXACT

  // ---- invite / login ----
  'invite.title': { uk: 'Вхід для викладача', en: 'Teacher sign-in' },
  'invite.lead': {
    uk: 'Використайте посилання-запрошення. Токен обробляється лише в пам’яті.',
    en: 'Use the invitation link. The token is processed in memory only.',
  },
  'invite.prompt': {
    uk: 'Тестовий токен (або залиште порожнім для автоматичного):',
    en: 'Test token (or leave blank for automatic):',
  },
  'invite.badToken': { uk: 'Некоректний формат токена.', en: 'Invalid token format.' },
  'invite.testBtn': {
    uk: 'Увійти за тестовим запрошенням (тест)',
    en: 'Sign in with a test invitation (test)',
  },
  'invite.small': {
    uk: 'У реальному сценарії — відкрийте посилання з #invite=...',
    en: 'In a real scenario — open a link with #invite=...',
  },
  'passkey.signIn': { uk: 'Увійти за ключем доступу', en: 'Sign in with a passkey' },
  'passkey.add': { uk: 'Додати ключ доступу', en: 'Add a passkey' },
  'passkey.authStartFailed': {
    uk: 'Не вдалося почати вхід за ключем доступу.',
    en: 'Could not start sign-in with a passkey.',
  },
  'passkey.enrollUnavailable': {
    uk: 'Ключ доступу зараз не можна додати.',
    en: 'A passkey cannot be added right now.',
  },
  'passkey.notConfirmed': {
    uk: 'Ключ доступу не підтверджено.',
    en: 'The passkey could not be confirmed.',
  },
  'passkey.recoveryCodesAlert': {
    uk: 'Збережіть ці коди відновлення; їх буде показано лише раз:\n\n{codes}',
    en: 'Save these recovery codes; they will be shown only once:\n\n{codes}',
  },
  'localAuthDisabled.banner': {
    uk: 'Локальний режим: автентифікацію вимкнено. Не відкривайте застосунок у мережі.',
    en: 'Local mode: authentication is disabled. Do not expose this app to a network.',
  },

  // ---- loading ----
  loading: { uk: 'Завантаження…', en: 'Loading…' },

  // ---- paste hub ----
  'paste.title': { uk: 'Створити новий урок', en: 'Create a new lesson' },
  'paste.disclose': {
    uk: 'Вставлений текст і матеріали згенерованих завдань можуть надсилатися зовнішнім провайдерам обраної моделі, зокрема резервному. Не використовуйте чутливі або персональні дані.',
    en: 'The pasted text and generated activity material may be sent to the selected model’s external providers, including its fallback. Do not use sensitive or personal data.',
  },
  'paste.discloseUrl': {
    uk: 'Текст із посилання і матеріали згенерованих завдань можуть надсилатися зовнішнім провайдерам обраної моделі, зокрема резервному. Не використовуйте чутливі або персональні дані.',
    en: 'The text from the link and generated activity material may be sent to the selected model’s external providers, including its fallback. Do not use sensitive or personal data.',
  },
  'paste.levelPre': { uk: 'Рівень: ', en: 'Level: ' }, // T_EXACT «Рівень»
  'paste.levelPost': { uk: ' (фіксовано для пілоту)', en: ' (fixed for the pilot)' },
  'paste.duration': { uk: 'Тривалість (хв)', en: 'Duration (min)' },
  'paste.model': { uk: 'Модель для уроку', en: 'Lesson model' },
  'paste.modelUnavailable': {
    uk: 'Немає моделей, кваліфікованих для поточних правил уроку.',
    en: 'No models are qualified for the current lesson rules.',
  },
  'paste.methodology': { uk: 'Методика', en: 'Methodology' },
  'paste.methodology.ttt': { uk: 'Тест → Навчання → Тест', en: 'Test → Teach → Test' },
  'paste.methodology.hint': { uk: 'перевірити → навчити → перевірити. Радимо для B1.', en: 'test → teach → re-test. Recommended for B1.' },
  'paste.focus': { uk: 'Граматичний фокус', en: 'Grammar focus' },
  'paste.focusPh': { uk: 'напр., вищий ступінь прикметників', en: 'e.g., comparative adjectives' }, // T_EXACT
  'paste.textLabel': { uk: 'Текст для уроку (вставте)', en: 'Text for the lesson (paste)' },
  'paste.textLabelReview': { uk: 'Текст для уроку (перегляньте)', en: 'Text for the lesson (review)' },
  'paste.restored': {
    uk: 'Текст попереднього запиту відновлено',
    en: 'Text from the previous request restored',
  },
  'paste.textPh': { uk: 'Вставте український текст...', en: 'Paste Ukrainian text...' },
  'paste.textRequired': {
    uk: 'Щоб згенерувати урок, вставте український текст.',
    en: 'To generate a lesson, paste Ukrainian text.',
  },
  'paste.submit': { uk: 'Згенерувати урок', en: 'Generate lesson' },
  'paste.submitting': { uk: 'Надсилаємо…', en: 'Sending…' },
  'close.aria': { uk: 'Закрити', en: 'Close' }, // T_EXACT

  // ---- catalog ----
  'catalog.title': { uk: 'Мої заняття', en: 'My lessons' }, // T_EXACT
  'catalog.refresh': { uk: 'Оновити список', en: 'Refresh list' },
  'catalog.new': { uk: '+ Нове заняття', en: '+ New lesson' },
  'catalog.emptyTitle': { uk: 'Поки що занять немає', en: 'No lessons yet' },
  'catalog.empty': { uk: 'Створіть перше заняття: дайте «Граматці» текст — і за кілька хвилин отримаєте готове заняття з перевіреними завданнями.', en: 'Create your first lesson: give Hramatka a text — and in a few minutes you get a ready lesson with verified tasks.' },
  'catalog.anchor': { uk: 'Якір: {snippet}', en: 'Anchor: {snippet}' },
  'catalog.meta': { uk: '{level} · {duration} хв · {methodology}', en: '{level} · {duration} min · {methodology}' },
  'catalog.conduct': { uk: 'Проведення', en: 'Run lesson' },
  'catalog.print': { uk: 'Друк', en: 'Print' },
  'settings.title': { uk: 'Налаштування', en: 'Settings' },
  'settings.defaultMethodology': { uk: 'Типова методика', en: 'Default methodology' },
  accepted: { uk: 'Прийнято', en: 'Accepted' },

  // ---- status chips (mirror app-helpers.statusLabel UA) ----
  'status.baking': { uk: 'готується', en: 'baking' }, // T_EXACT «готується…»
  'status.ready': { uk: 'готово', en: 'ready' }, // T_EXACT
  'status.failed': { uk: 'помилка', en: 'error' },
  'status.draft': { uk: 'чернетка', en: 'draft' }, // T_EXACT

  // ---- lesson view toolbar ----
  'lesson.back': { uk: '← До списку', en: '← To the list' },
  'lesson.reviewMode': { uk: 'Режим огляду', en: 'Review mode' },
  'lesson.runMode': { uk: 'Режим запуску (для учня)', en: 'Run mode (for the student)' },
  'lesson.showAsStudent': { uk: '👩‍🎓 Показати як учневі', en: '👩‍🎓 Show as student' }, // #112 enter-student-mode; demo UI «Показати як учневі»
  'lesson.hideAnswers': { uk: 'Сховати відповіді', en: 'Hide answers' }, // T_EXACT
  'lesson.showAnswers': { uk: 'Показати відповіді', en: 'Show answers' },
  'lesson.copy': { uk: 'Копіювати урок', en: 'Copy lesson' }, // cf. T_EXACT «⧉ Копіювати»
  'lesson.conduct': { uk: '▶ Провести заняття', en: '▶ Run the lesson' },
  'lesson.print': { uk: 'Друк', en: 'Print' }, // T_EXACT «🖨 Друк»
  'lesson.downloadJson': { uk: 'Завантажити JSON', en: 'Download JSON' },
  // #401 lesson provenance: which logical model baked this lesson (id carries the version).
  'lesson.model': { uk: 'Модель: {model}', en: 'Model: {model}' },
  'lesson.modelUnknown': { uk: 'невідомо', en: 'unknown' },

  // ---- student widget feedback (#410) ----
  'studentActivity.check': { uk: 'Перевірити', en: 'Check' },
  'studentActivity.retry': { uk: 'Спробувати ще раз', en: 'Try again' },
  'studentActivity.correct': { uk: '✓ Правильно', en: '✓ Correct' },
  'studentActivity.incorrect': { uk: '✗ Спробуйте ще раз', en: '✗ Try again' },
  'studentActivity.true': { uk: 'Правда', en: 'True' },
  'studentActivity.false': { uk: 'Неправда', en: 'False' },
  'studentActivity.choose': { uk: 'Оберіть відповідь', en: 'Choose an answer' },
  'studentActivity.fillBlank': { uk: 'Пропуск {n}', en: 'Blank {n}' },
  'studentActivity.clozeBlank': { uk: 'Пропуск {n}', en: 'Blank {n}' },
  'studentActivity.findError': { uk: 'Крок 1: знайдіть помилку в реченні.', en: 'Step 1: find the error in the sentence.' },
  'studentActivity.chooseCorrection': { uk: 'Оберіть правильну форму для «{word}»', en: 'Choose the correct form for “{word}”' },
  'studentActivity.guidance': { uk: 'Для обговорення / оцінювання:', en: 'For discussion / grading:' },
  'studentActivity.yourResponse': { uk: 'Ваша відповідь:', en: 'Your response:' },
  'studentActivity.writingPlaceholder': { uk: 'Напишіть відповідь тут…', en: 'Write your response here…' },

  // Clipboard export (PR #120 folded into #114 i18n): teacher/student variants + copy-as-new action.
  // Lesson CONTENT text (anchor + activities) stays UA; only chrome labels/notices are translated here.
  'lesson.copyAsNew': { uk: 'Створити інший урок із цього тексту', en: 'Create another lesson from this text' },
  'lesson.copyTeacher': { uk: 'Копіювати для вчителя', en: 'Copy for teacher' },
  'lesson.copyStudent': { uk: 'Копіювати для учня', en: 'Copy for student' },

  // transient clipboard chrome notices (success/fail banners)
  'clipboard.copiedTeacher': {
    uk: 'Скопійовано: урок для вчителя (із відповідями).',
    en: 'Copied: lesson for teacher (with answers).',
  },
  'clipboard.copiedStudent': {
    uk: 'Скопійовано: урок для учня (без відповідей). Можна вставити в чат Zoom.',
    en: 'Copied: lesson for student (no answers). You can paste into a Zoom chat.',
  },
  'clipboard.copyFailed': { uk: 'Не вдалося скопіювати. Спробуйте ще раз.', en: 'Could not copy. Please try again.' },

  // ---- baking card ----
  'bake.statusPrefix': { uk: 'Статус: ', en: 'Status: ' },
  'bake.failFallback': { uk: 'Не вдалося створити урок.', en: 'The lesson could not be created.' },
  // Generic fallback (unknown codes) — overload framing kept for backward-compatible unknown path.
  'recovery.body': {
    uk: 'Не вдалося створити урок. Таке інколи трапляється, коли сервіс перевантажений. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. This sometimes happens when the service is overloaded. Try again — your text is already saved.',
  },
  // Per-failure_code recovery copy (#185). Source-capacity guidance is reserved
  // for the deterministic preflight code; post-generation floors keep a retry CTA.
  'recovery.body.insufficient_anchor_capacity': {
    uk: 'Опорного матеріалу недостатньо для повного уроку. Спробуйте довший і різноманітніший текст із конкретними деталями — ваш текст уже збережено.',
    en: 'The source does not contain enough supported material for a complete lesson. Use a longer, more varied source with concrete details — your text is already saved.',
  },
  'recovery.body.lesson_floor_unmet': {
    uk: 'Цього разу не вдалося скласти повний урок. Спробуйте ще раз — ваш текст уже збережено.',
    en: 'A complete lesson could not be created this time. Try again — your text is already saved.',
  },
  'recovery.body.engine_unavailable': {
    uk: 'Не вдалося створити урок. Таке інколи трапляється, коли сервіс перевантажений. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. This sometimes happens when the service is overloaded. Try again — your text is already saved.',
  },
  'recovery.body.provider_unavailable': {
    uk: 'Не вдалося створити урок. Таке інколи трапляється, коли сервіс перевантажений. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. This sometimes happens when the service is overloaded. Try again — your text is already saved.',
  },
  'recovery.body.bake_timeout': {
    uk: 'Не вдалося створити урок — перевищено час очікування. Спробуйте ще раз — текст уже збережено.',
    en: 'Could not create the lesson — the wait time was exceeded. Try again — your text is already saved.',
  },
  'recovery.body.lesson_schema_invalid': {
    uk: 'Не вдалося створити урок. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. Try again — your text is already saved.',
  },
  'recovery.body.worker_restarted': {
    uk: 'Не вдалося створити урок. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. Try again — your text is already saved.',
  },
  'recovery.body.unknown_safe_failure': {
    uk: 'Не вдалося створити урок. Спробуйте ще раз — текст уже збережено.',
    en: 'The lesson could not be created. Try again — your text is already saved.',
  },
  'recovery.body.generation_failed': {
    uk: 'Не вдалося обробити відповідь генератора. Спробуйте ще раз — текст уже збережено.',
    en: 'Could not process the generator response. Try again — your text is already saved.',
  },
  'recovery.body.no_eligible_activities': {
    uk: 'Не вдалося підібрати вправи для цього тексту. Додайте більше деталей або оберіть інший текст — текст уже збережено.',
    en: 'Could not select exercises for this text. Add more detail or choose different text — your text is already saved.',
  },
  'recovery.retry': {
    uk: 'Створити урок ще раз із цим текстом',
    en: 'Create the lesson again from this text',
  },
  'recovery.back': { uk: 'Повернутися до списку', en: 'Back to the list' },
  'bake.updating': { uk: 'Оновлення…', en: 'Updating…' },
  'bake.checkNow': { uk: 'Перевірити зараз', en: 'Check now' },
  'bake.elapsed': { uk: 'Минуло {clock}', en: 'Elapsed {clock}' }, // #112 honest live wait clock (no demo equivalent)

  // ---- lesson meta ----
  'meta.level': {
    uk: 'Рівень: {level} • Тривалість: {duration} хв',
    en: 'Level: {level} • Duration: {duration} min',
  },
  'meta.revision': {
    uk: 'Ревізія: {rev} • Прийнято: {acc}',
    en: 'Revision: {rev} • Accepted: {acc}',
  },
  'meta.yes': { uk: 'так', en: 'yes' },
  'meta.no': { uk: 'ні', en: 'no' },
  'meta.focus': { uk: 'Фокус: ', en: 'Focus: ' }, // T_EXACT «Граматичний фокус»

  // ---- accept bar / run note ----
  'accept.warnNote': {
    uk: 'Потрібно підтвердити всі попередження (⚠️), щоб прийняти урок.',
    en: 'You must confirm all warnings (⚠️) to accept the lesson.',
  },
  'accept.btn': { uk: 'Прийняти урок (ревізія {rev})', en: 'Accept lesson (revision {rev})' },
  'accept.draft': { uk: 'Повернути в чернетку', en: 'Return to draft' },
  'run.note': {
    uk: 'Це режим для демонстрації учню — відповіді та ключі приховані (залежно від віджета).',
    en: 'This mode is for showing the student — answers and keys are hidden (depending on the widget).',
  },

  // ---- student run surface (#112 student-share chrome; EN from demo UI/T_EXACT) ----
  'chip.student': { uk: '👩‍🎓 УЧЕНЬ', en: '👩‍🎓 STUDENT' }, // demo UI chip.student
  'run.studentBanner': {
    uk: '👩‍🎓 ЕКРАН УЧНЯ — без відповідей і підказок. Безпечно ділитися в Zoom.',
    en: '👩‍🎓 STUDENT SCREEN — no answers, no hints. Safe to share in Zoom.',
  }, // demo UI banner.student
  'run.toolbarChip': {
    uk: '{level} · готово · {duration} хв',
    en: '{level} · ready · {duration} min',
  }, // demo UI chip.ready / chip.min
  'run.backToTeacher': { uk: '👩‍🏫 Назад до екрана вчителя', en: '👩‍🏫 Back to teacher screen' }, // demo UI btn.back
  'run.sheetSub': {
    uk: 'Тест → Навчання → Тест · ≈ {duration} хв',
    en: 'Test → Teach → Test · ≈ {duration} min',
  }, // demo T_EXACT «Тест → Навчання → Тест»

  // ---- lesson blocks (phase renderer) ----
  'blocks.phase': { uk: 'Фаза {phase}', en: 'Phase {phase}' },
  'blocks.warnBadge': { uk: '⚠️ попередження', en: '⚠️ warning' },
  // #164: error-correction prints deliberately wrong Ukrainian; teachers read it as an
  // engine defect. Chrome, so dual-lang — the marked-up forms below it stay lesson content.
  'blocks.deliberateError': { uk: 'Навмисна помилка', en: 'Deliberate mistake' },
  'blocks.externalOptions': { uk: 'зовнішні варіанти', en: 'external options' },
  'blocks.answerKey': { uk: 'Ключ відповіді:', en: 'Answer key:' }, // cf. T_EXACT «🔑 Відповіді»
  'blocks.note': { uk: 'Примітка: ', en: 'Note: ' },
  'blocks.provenance': {
    uk: 'Походження: {source} • {generator}',
    en: 'Origin: {source} • {generator}',
  },
  'blocks.ackBtn': { uk: 'Підтвердити попередження', en: 'Confirm warning' },
  'blocks.acked': { uk: '✓ Підтверджено', en: '✓ Confirmed' },

  // ---- footer ----
  footer: {
    uk: 'Приватний пілот • Тільки для запрошених викладачів • B1',
    en: 'Private pilot • Invited teachers only • B1',
  },
  'print.honestyFooter': {
    uk: 'Складено з тексту вчителя · мову перевірено за словником · факти перевірте · Граматка',
    en: 'Compiled from the teacher’s text · language checked against dictionary · verify facts · Hramatka',
  },

  // ---- client-set error chrome (server messages flow through as raw, never here) ----
  'err.sessionRequired': { uk: 'Потрібна сесія викладача.', en: 'A teacher session is required.' },
  'err.badTextLen': {
    uk: 'Текст має бути від 1 до 100000 символів.',
    en: 'The text must be 1 to 100000 characters.',
  },
  'err.bakeFailed': {
    uk: 'Не вдалося скласти урок. Спробуйте, будь ласка, ще раз.',
    en: 'Could not build the lesson. Please try again.',
  },
  'err.noRetrySource': {
    uk: 'Текст уроку недоступний. Вставте текст знову на головній сторінці.',
    en: 'The lesson text is unavailable. Paste the text again on the main page.',
  },
  'err.copyAsNewNoAnchor': {
    uk: 'Текст уроку недоступний для копіювання. Спробуйте оновити сторінку.',
    en: 'The lesson text is unavailable to copy. Try refreshing the page.',
  },
  'err.inviteGone': { uk: 'Запрошення більше недоступне.', en: 'The invitation is no longer available.' },
  'err.inviteBadToken': { uk: 'Некоректний токен запрошення.', en: 'Invalid invitation token.' },
  'err.loginFailed': { uk: 'Помилка входу', en: 'Sign-in error' },
  'err.initSession': {
    uk: 'Помилка ініціалізації сесії. Спробуйте перезавантажити сторінку.',
    en: 'Session initialization error. Try reloading the page.',
  },
  'err.longWait': {
    uk: 'Тривале очікування. Спробуйте оновити сторінку.',
    en: 'This is taking a while. Try reloading the page.',
  },
  'err.lessonNotFound': { uk: 'Урок не знайдено.', en: 'Lesson not found.' },
  'err.generic': { uk: 'Помилка', en: 'Error' },
  'err.genericDot': { uk: 'Помилка.', en: 'Error.' },
  'err.loadLesson': {
    uk: 'Не вдалося завантажити урок. Спробуйте, будь ласка, ще раз.',
    en: 'Could not load the lesson. Please try again.',
  },
  'err.lessonChangedReload': { uk: 'Урок змінився. Перезавантажте.', en: 'The lesson has changed. Reload.' },
  'err.ackFailed': { uk: 'Не вдалося підтвердити.', en: 'Could not confirm.' },
  'err.ackAllBeforeAccept': {
    uk: 'Підтвердіть усі попередження перед прийняттям уроку.',
    en: 'Confirm all warnings before accepting the lesson.',
  },
  'err.ackAllBeforeAcceptShort': {
    uk: 'Підтвердіть усі попередження перед прийняттям.',
    en: 'Confirm all warnings before accepting.',
  },
  'err.lessonChangedReload2': { uk: 'Урок змінився — перезавантажте.', en: 'The lesson has changed — reload.' },
  'err.acceptFailed': { uk: 'Не вдалося прийняти.', en: 'Could not accept.' },
  'err.draftFailed': { uk: 'Не вдалося повернути в чернетку.', en: 'Could not return to draft.' },
  'err.sessionExpired': { uk: 'Сесія закінчилась. Увійдіть знову.', en: 'Your session has ended. Sign in again.' },

  // ---- URL import (#118) client-set errors (server e.message flows through as raw) ----
  'err.urlSourceRequired': {
    uk: 'Потрібна адреса джерела для уроку з посилання.',
    en: 'A source address is required for a lesson from a link.',
  },
  'err.urlNeedAddress': {
    uk: 'Вставте адресу сторінки — і ми дістанемо з неї текст.',
    en: 'Paste a page address — and we will extract the text from it.',
  },
  'err.urlFetchFailed': {
    uk: 'Не вдалося отримати текст із посилання.',
    en: 'Could not fetch text from the link.',
  },

  // =====================================================================
  // Conductor (▶ Проведення заняття) — chrome from demo CONDT (verbatim EN)
  // =====================================================================
  'cond.empty.title': { uk: 'Немає активного заняття', en: 'No active lesson' },
  'cond.empty.body': {
    uk: 'Відкрийте прийняте заняття й натисніть «▶ Провести заняття».',
    en: 'Open an accepted lesson and press “▶ Run the lesson”.',
  },
  'cond.empty.toList': { uk: 'До списку', en: 'To the list' },
  'cond.ph1': { uk: 'Тест 1', en: 'Test 1' }, // CONDT
  'cond.ph2': { uk: 'Навчання', en: 'Teaching' }, // CONDT
  'cond.ph3': { uk: 'Тест 2', en: 'Test 2' }, // CONDT
  'cond.min': { uk: 'хв', en: 'min' }, // CONDT
  'cond.budget': { uk: 'бюджет', en: 'budget' }, // CONDT
  'cond.break': { uk: 'перерва', en: 'break' }, // CONDT
  'cond.behind': { uk: '⏱ Відстаємо від часу.', en: '⏱ Running behind.' }, // CONDT
  'cond.shorten': { uk: 'Скоротити план', en: 'Shorten the plan' }, // CONDT
  'cond.removeType': { uk: '(прибрати «{type}»)', en: '(remove “{type}”)' },
  'cond.pfTitle': { uk: '🔎 Попередній прогін', en: '🔎 Preflight' }, // CONDT
  'cond.example': { uk: 'приклад', en: 'example' }, // CONDT
  'cond.pfReady': { uk: '{n} готові', en: '{n} ready' }, // CONDT pfOk
  'cond.pfFlag': { uk: '{n} варто глянути', en: '{n} to double-check' }, // CONDT pfFlag
  'cond.pfHeld': { uk: '{n} відкладено', en: '{n} set aside' }, // CONDT pfHeld
  'cond.pfSub': {
    uk: 'приклад майбутньої функції: перед заняттям «Граматка» зможе прогнати його з кількома змодельованими учнями рівня B1 — щоб зловити хитрі місця заздалегідь. Тут показано, як це виглядатиме',
    en: 'example of a future feature: before the lesson Hramatka will be able to dry-run it with a few simulated B1 learners — to catch tricky spots in advance. Shown here is how that will look',
  }, // CONDT
  'cond.pfItemDefault': { uk: 'позначено на ваш перегляд', en: 'flagged for your review' },
  'cond.endOfPlan': { uk: 'Кінець плану.', en: 'End of the plan.' },
  'cond.taskCount': { uk: 'Завдання {i} з {total}', en: 'Task {i} of {total}' }, // CONDT task/of
  'cond.panelLabel': {
    uk: '🔒 Ваша панель — учень цього не бачить',
    en: '🔒 Your panel — the student does not see this',
  }, // CONDT
  'cond.markVerified': { uk: '✓ перевірено', en: '✓ verified' }, // CONDT mVer
  'cond.markLook': { uk: '⚠ погляньте', en: '⚠ take a look' }, // CONDT mLook
  'cond.answerLabel': { uk: '🔑 Відповідь', en: '🔑 Answer' }, // CONDT answer
  'cond.whyHead': { uk: '📎 ЧОМУ ЦЕ ЗАВДАННЯ ІСНУЄ', en: '📎 WHY THIS TASK EXISTS' }, // CONDT whyHead
  'cond.rSrc': { uk: 'Джерело:', en: 'Source:' }, // CONDT rSrc
  'cond.rWhy': { uk: 'Навіщо:', en: 'Purpose:' }, // CONDT rWhy
  'cond.rConf': { uk: 'Впевненість:', en: 'Confidence:' }, // CONDT rConf
  'cond.rFlagged': { uk: 'Позначено:', en: 'Flagged:' }, // CONDT rRej
  'cond.sharedLabel': {
    uk: '👩‍🎓 Спільний екран — це бачить учень',
    en: '👩‍🎓 Shared screen — the student sees this',
  }, // CONDT shared
  'cond.backToPanel': { uk: '✏ Повернутися до панелі', en: '✏ Back to your panel' }, // CONDT exitPreview
  'cond.back': { uk: '← Назад', en: '← Back' }, // CONDT back
  'cond.showAnsBtn': { uk: '🔑 Відповідь', en: '🔑 Answer' }, // CONDT showAns
  'cond.hideAnsBtn': { uk: '🔑 Сховати', en: '🔑 Hide' }, // CONDT hideAns
  'cond.whyBtn': { uk: 'ⓘ Чому це завдання?', en: 'ⓘ Why this task?' }, // CONDT why
  'cond.tooEasy': { uk: '🙂 Занадто легко', en: '🙂 Too easy' }, // CONDT easy
  'cond.checkWrong': { uk: '⚑ Перевірка помилилася', en: '⚑ Check was wrong' }, // CONDT gate
  'cond.skip': { uk: '⤼ Пропустити', en: '⤼ Skip' }, // CONDT skip
  'cond.done': { uk: '✓ Готово', en: '✓ Done' }, // CONDT done
  'cond.summary.title': { uk: 'Що сталося на занятті', en: 'What happened in the lesson' }, // CONDT sumTitle
  'cond.summary.lead': {
    uk: 'Приклад: так виглядатиме запис, який «Граматка» зможе зберігати після кожного заняття (за згодою учня). Персональних даних учня зараз не зберігаємо.',
    en: 'Example: this is how the record will look — Hramatka will be able to keep one after every lesson (with the student’s consent). No personal student data is stored now.',
  }, // CONDT sumLead
  'cond.sDone': { uk: 'виконано', en: 'done' }, // CONDT sDone
  'cond.sSkip': { uk: 'пропущено', en: 'skipped' }, // CONDT sSkip
  'cond.sEasy': { uk: 'легкі', en: 'too easy' }, // CONDT sEasy
  'cond.sFlag': { uk: '«перевірка помилилася»', en: '“check was wrong” flags' }, // CONDT sFlag
  'cond.phaseTime': { uk: 'Час за частинами', en: 'Time by phase' }, // CONDT phaseTime
  'cond.tblPhase': { uk: 'Частина', en: 'Phase' }, // CONDT phaseC
  'cond.tblPlan': { uk: 'план', en: 'plan' }, // CONDT planned
  'cond.tblActual': { uk: 'факт', en: 'actual' }, // CONDT actual
  'cond.flagsHead': { uk: 'Позначки для «Граматки»', en: 'Flags for Hramatka' }, // CONDT flagsHead
  'cond.noFlags': { uk: 'Позначок немає — усе пройшло гладко.', en: 'No flags — everything went smoothly.' }, // CONDT noFlags
  'cond.ruleOk': {
    uk: '✓ Приклад: так це правило запамʼяталося б у памʼятці про цього учня (у демо нічого не зберігається).',
    en: '✓ Example: this is how the rule would be saved to this learner’s memory (nothing is stored in the demo).',
  }, // CONDT ruleOk
  'cond.ruleTitle': {
    uk: '💡 Правило для «Граматки» (приклад майбутньої функції)',
    en: '💡 A rule for Hramatka (example of a future feature)',
  }, // CONDT ruleTitle
  'cond.ruleText': {
    uk: '«Для цього учня вводьте вищий ступінь прикметників контрастно (більший ↔ менший).»',
    en: '“For this learner, introduce comparative adjectives contrastively (bigger ↔ smaller).”',
  }, // CONDT ruleText
  'cond.ruleNote': {
    uk: 'Це нотатка про ЦЬОГО учня, а не загальне правило.',
    en: 'This is a note about THIS learner, not a global rule.',
  }, // CONDT ruleNote
  'cond.ruleAcc': { uk: 'Прийняти правило', en: 'Accept rule' }, // CONDT ruleAcc
  'cond.ruleRej': { uk: 'Не треба', en: 'Dismiss' }, // CONDT ruleRej
  'cond.again': { uk: '↺ Провести ще раз', en: '↺ Run again' }, // CONDT again
  'cond.toReady': { uk: 'До готового заняття', en: 'Back to the ready lesson' }, // CONDT toReady
  'cond.studBanner': {
    uk: '👩‍🎓 ЕКРАН УЧНЯ — без відповідей і підказок. Безпечно ділитися в Zoom.',
    en: '👩‍🎓 STUDENT SCREEN — no answers or hints. Safe to share in Zoom.',
  }, // CONDT studBanner
  'cond.heldLabel': { uk: 'Відкладено на перевірку:', en: 'Set aside for review:' }, // CONDT held
  'cond.heldRest': {
    uk: '{n} завдання, що не пройшли перевірку, — до заняття не ввійшли (їх видно у «Перегляді»).',
    en: '{n} task(s) that failed verification did not enter the lesson (visible in “Review”).',
  }, // CONDT holdWho
  'cond.hint': {
    uk: '💡 Ви проводите вже готове й перевірене заняття, крок за кроком. Смужка вгорі — три частини й час на кожну. «ⓘ Чому це завдання?» показує, звідки воно взялося. «＋5 хв» пришвидшує годинник, щоб побачити, як план підлаштовується під час.',
    en: '💡 You are running an already-built, verified lesson, step by step. The bar on top is the three phases and the time budget for each. “ⓘ Why this task?” shows where it came from. “＋5 min” fast-forwards the clock so you can see the plan adapt.',
  }, // CONDT hint
  'cond.hideHint': { uk: 'сховати підказку', en: 'hide this hint' }, // T_EXACT
  'cond.top.title': { uk: '▶ Проведення заняття', en: '▶ Running the lesson' }, // CONDT title
  'cond.exit': { uk: '← Вийти', en: '← Exit' }, // CONDT exit
  'cond.addTime': { uk: '＋5 хв ⏱', en: '＋5 min ⏱' }, // CONDT addTime
  'cond.profileBtn': { uk: '👤 Профіль учня', en: '👤 Learner profile' }, // CONDT profile
  'cond.studentView': { uk: '👁 Як бачить учень', en: '👁 Student view' }, // CONDT preview
  'cond.helpBtn': { uk: '? Довідка', en: '? Help' }, // CONDT help
  'cond.future': { uk: 'МАЙБУТНЄ', en: 'FUTURE' }, // CONDT profFuture
  'cond.profLead': {
    uk: 'Памʼять про учня, зібрана з його реальних відповідей — щоб наступні заняття були під нього, а не просто «рівня B1». Зʼявляється лише після реєстрації учня, за згодою.',
    en: 'A memory of the learner, built from their real answers — so future lessons fit them, not just “B1 level”. Appears only after the student registers, with consent.',
  }, // CONDT profLead
  'cond.ledgerNote': {
    uk: '🔒 Ця памʼять зʼявиться, коли учень зареєструється — за його згодою. Поки що її бачите тільки ви.',
    en: '🔒 This memory appears once the student registers — with their consent. For now only you see it.',
  }, // CONDT ledgerNote
  'cond.close': { uk: 'Закрити', en: 'Close' }, // CONDT close
  'cond.help.title': { uk: 'Довідка — режим «Проведення заняття»', en: 'Help — “Running the lesson” mode' }, // CONDT helpTitle
  'cond.help.q1': { uk: 'Що це?', en: 'What is this?' },
  'cond.help.a1': {
    uk: '«Проведення заняття» — режим, у якому ви <b>проводите вже готове й перевірене заняття</b> просто на екрані під час уроку (наприклад, у Zoom). Завдання показуються по одному. «Граматка» не звертається до штучного інтелекту наживо — заняття вже складене й заморожене.',
    en: '“Running the lesson” is a mode where you <b>run an already-built, verified lesson</b> right on the screen during the class (for example, in Zoom). Tasks are shown one at a time. Hramatka does not call AI live — the lesson is already built and frozen.',
  },
  'cond.help.q2': { uk: 'Спільний екран', en: 'Shared screen' },
  'cond.help.a2': {
    uk: 'Центральний блок із зеленою рамкою — це те саме, що бачить учень: інтерактивне завдання без відповідей. У Zoom є три способи: ви натискаєте самі (учень відповідає усно); або ділитеся <b>чистим екраном учня</b> — кнопка «👁 Як бачить учень» ховає вашу панель; або даєте учневі посилання, щоб натискав він сам.',
    en: 'The central block with the green border is exactly what the student sees: an interactive task without answers. In Zoom there are three ways: you click yourself (the student answers aloud); or you share a <b>clean student screen</b> — the “👁 Student view” button hides your panel; or you give the student a link so they click themselves.',
  },
  'cond.help.q3': { uk: 'Ваша панель', en: 'Your panel' },
  'cond.help.a3': {
    uk: 'Жовта панель під завданням — приватна, учень її не бачить: 🔑 відповідь, позначка перевірки, ⓘ «чому це завдання», і кнопки керування.',
    en: 'The wheat panel under the task is private — the student does not see it: 🔑 the answer, the verification mark, ⓘ “why this task”, and the control buttons.',
  },
  'cond.help.q4': { uk: 'Смужка часу вгорі', en: 'The time bar on top' },
  'cond.help.a4': {
    uk: 'Три частини заняття (Тест → Навчання → Тест) і час на кожну. Годинник іде для поточної частини; якщо перевищуєте бюджет — він стає жовтим і зʼявляється підказка «скоротити».',
    en: 'The three phases of the lesson (Test → Teach → Test) and the time for each. The clock runs for the current phase; if you go over budget it turns amber and a “shorten” hint appears.',
  },
  'cond.help.q5': { uk: 'Кнопки під завданням', en: 'The buttons under the task' },
  'cond.help.a5': {
    uk: '<b>Готово</b> — далі · <b>Пропустити</b> · <b>Занадто легко</b> — для цього учня · <b>Перевірка помилилася</b> — коли перевірка дарма щось позначила · <b>🔑</b> — відповідь бачите тільки ви · <b>ⓘ Чому це завдання?</b> — звідки воно взялося.',
    en: '<b>Done</b> — next · <b>Skip</b> · <b>Too easy</b> — for this learner · <b>Check was wrong</b> — when the check flagged something for nothing · <b>🔑</b> — only you see the answer · <b>ⓘ Why this task?</b> — where it came from.',
  },
  'cond.help.q6': { uk: '🔎 Попередній прогін · 🛑 Відкладено', en: '🔎 Preflight · 🛑 Set aside' },
  'cond.help.a6': {
    uk: '<i>Приклад майбутньої функції:</i> перед заняттям система зможе «прогнати» його з кількома змодельованими учнями рівня B1, щоб зловити хитрі місця заздалегідь. А що не пройшло перевірку — не потрапляє в заняття, а чекає на ваш перегляд. Нічого не зникає тихо.',
    en: '<i>Example of a future feature:</i> before the lesson the system will be able to “dry-run” it with a few simulated B1 learners to catch tricky spots in advance. And whatever fails verification does not enter the lesson but waits for your review. Nothing disappears silently.',
  },

  // ---- conductor receipt (getReceipt) ----
  'cond.rc.srcWarn': {
    uk: 'Складено навколо тексту вчителя; частину (варіанти чи означення) додала «Граматка» — не з тексту',
    en: 'Built around the teacher’s text; part (options or definitions) was added by Hramatka — not from the text',
  },
  'cond.rc.srcOk': { uk: 'Складено з тексту вчителя', en: 'Built from the teacher’s text' },
  'cond.rc.why1': {
    uk: 'Перевірити розуміння прочитаного (Тест 1)',
    en: 'Check reading comprehension (Test 1)',
  },
  'cond.rc.why2': { uk: 'Відпрацювати мовну ціль (Навчання)', en: 'Practice the language target (Teaching)' },
  'cond.rc.why3': { uk: 'Закріпити й перевірити ще раз (Тест 2)', en: 'Reinforce and re-test (Test 2)' },
  'cond.rc.whyDefault': { uk: 'Частина заняття', en: 'Part of the lesson' },
  'cond.rc.confMedium': { uk: 'середня', en: 'medium' },
  'cond.rc.confHigh': { uk: 'висока', en: 'high' },
  'cond.rc.rejDefault': { uk: 'позначено ⚠ на ваш перегляд', en: 'flagged ⚠ for your review' },

  // ---- conductor summary flag templates ----
  'cond.flag.gate': {
    uk: '⚑ «{type}» — перевірка помилилася (перевірити правило)',
    en: '⚑ “{type}” — check was wrong (review the rule)',
  },
  'cond.flag.easy': {
    uk: '🙂 «{type}» — занадто легко для цього учня',
    en: '🙂 “{type}” — too easy for this learner',
  },
  'cond.flag.skip': { uk: '⤼ «{type}» — пропущено на занятті', en: '⤼ “{type}” — skipped in the lesson' },
  'cond.flag.dropped': {
    uk: '✂ «{type}» — прибрано із плану через брак часу (у запас/на домашнє)',
    en: '✂ “{type}” — removed from the plan for lack of time (to reserve/homework)',
  },

  // =====================================================================
  // Ahead of PR #109 (branch cursor/hramatka-client-gaps) — dictionary
  // entries folded in early so the merge only swaps literals for t().
  // =====================================================================
  'print.teacher': { uk: 'Друк для вчителя', en: 'Print for teacher' },
  'print.student': { uk: 'Друк для учня', en: 'Print for student' },
  'anchor.summary': { uk: 'Текст', en: 'Text' },
  'anchor.hide': { uk: 'Сховати текст', en: 'Hide text' },
  'anchor.readingHead': { uk: 'Текст для читання', en: 'Reading text' },

  // ---- URL import (#118): source tabs + fetch control (fetched anchor TEXT stays UA content) ----
  'anchor.sourceAria': { uk: 'Джерело тексту', en: 'Text source' },
  'anchor.tabText': { uk: 'Вставити текст', en: 'Paste text' },
  'anchor.tabUrl': { uk: 'З посилання', en: 'From a link' },
  'anchor.urlPh': {
    uk: 'https://… адреса статті чи оголошення',
    en: 'https://… article or listing address',
  },
  'anchor.fetchBtn': { uk: 'Отримати текст', en: 'Fetch text' },
  'anchor.fetching': { uk: 'Отримуємо…', en: 'Fetching…' },

  // ---- review workbench chrome (re-land #115 + fold into t() for #121) ----
  // margin chips, rejected/reserve trays, duration/reserve controls, accept/save labels, editor buttons, per-type, provenance
  'review.banner': { uk: 'Мову й відповідність вашому тексту ми перевірили автоматично; зміст і доречність — за вами. Ви — вчитель, «Граматка» — помічниця.', en: 'We checked the language and fidelity to your text automatically; the content and appropriateness are up to you. You are the teacher, Hramatka is the assistant.' },
  'review.sub': { uk: 'Заняття — це документ. Позначки перевірки — на полях. Усе можна редагувати просто тут.', en: 'The lesson is a document. Verification marks are in the margins. Everything can be edited right here.' },
  'review.durationLabel': { uk: 'Тривалість:', en: 'Duration:' },
  'review.durationHint': { uk: '— план ріжеться й росте на очах; зрізане не зникає', en: '— the plan shrinks and grows live; what is cut does not disappear' },
  'review.emptyPhase': { uk: '— порожньо; пересуньте сюди завдання (↑↓) —', en: '— empty; move tasks here (↑↓) —' },
  'review.noWidget': { uk: 'Чернетка без віджета', en: 'Draft without widget' },
  'review.unavailable': { uk: 'Редагування цього типу недоступне.', en: 'Editing for this type is not available.' },
  'chip.verified': { uk: '✓ перевірено', en: '✓ verified' },
  'chip.confirmed': { uk: '✓ підтверджено', en: '✓ confirmed' },
  'chip.look': { uk: '⚠ погляньте', en: '⚠ review' },
  'chip.edited': { uk: '✎ змінено вами', en: '✎ edited by you' },
  'review.reserveHead': { uk: 'У запасі — не входить у {duration}-хвилинний план · підійде на домашнє або на довше заняття:', en: 'In reserve — does not fit the {duration}-minute plan · suitable for homework or a longer lesson:' },
  'review.include': { uk: 'включити в план', en: 'include in plan' },
  'review.rejectedHead': { uk: 'Ще {count} чернет{plural} не пройшл{ending} перевірку — ', en: 'Still {count} draft{plural} did not pass verification — ' },
  'review.rejectedHeadOne': { uk: 'Ще {count} чернетка не пройшла перевірку — ', en: 'Still {count} draft did not pass verification — ' },
  'review.rejectedHeadMany': { uk: 'Ще {count} чернетки не пройшли перевірку — ', en: 'Still {count} drafts did not pass verification — ' },
  'review.show': { uk: 'показати', en: 'show' },
  'review.hide': { uk: 'сховати', en: 'hide' },
  'review.rejectedChip': { uk: '✕ відхилено', en: '✕ rejected' },
  'review.restore': { uk: 'повернути в заняття', en: 'return to lesson' },
  'review.edit': { uk: 'редагувати', en: 'edit' },
  'review.up': { uk: 'вгору', en: 'up' },
  'review.down': { uk: 'вниз', en: 'down' },
  'review.remove': { uk: 'вилучити', en: 'remove' },
  'review.ack': { uk: 'підтвердити', en: 'acknowledge' },
  // Focus-notice chrome (#191). The notice body itself is engine-authored UA
  // lesson content rendered verbatim — only these labels are dual-lang.
  'review.focusNotice': { uk: 'Фокус не підкріплено опорою', en: 'Focus not supported by the text' },
  'review.focusNoticeRequested': { uk: 'Запит: ', en: 'Requested: ' },
  'review.focusNoticeAck': { uk: 'зрозуміло, підтверджую', en: 'understood, acknowledge' },
  'review.focusNoticeAcked': { uk: '✓ підтверджено', en: '✓ confirmed' },
  'review.acceptWarn': { uk: 'Потрібно підтвердити всі попередження (⚠), щоб прийняти урок.', en: 'You must confirm all warnings (⚠) to accept the lesson.' },
  'review.accept': { uk: 'Прийняти заняття', en: 'Accept lesson' },
  'review.saveDraft': { uk: 'Зберегти як чернетку', en: 'Save as draft' },
  'review.source': { uk: 'Джерело: ', en: 'Source: ' },
  'review.generator': { uk: 'генератор: ', en: 'generator: ' },
  'review.checks': { uk: 'Перевірки: ', en: 'Checks: ' },
  'review.external': { uk: 'Зовнішні варіанти — перевірте', en: 'External options — please review' },
  'review.answerKey': { uk: 'Ключ відповіді:', en: 'Answer key:' },
  // answer-key display chrome (#186) — content values stay UA; labels are dual-lang
  'answerKey.itemLine': {
    uk: 'Питання {n} → правильна відповідь: {value}',
    en: 'Question {n} → correct answer: {value}',
  },
  'answerKey.blankLine': { uk: 'Прогалина {n}: {value}', en: 'Gap {n}: {value}' },
  'answerKey.simpleLine': { uk: '{n}. {value}', en: '{n}. {value}' },
  'answerKey.pairLine': { uk: '{n}. {left} — {right}', en: '{n}. {left} — {right}' },
  'answerKey.wordsLine': { uk: 'Слова: {value}', en: 'Words: {value}' },
  'answerKey.guidanceLine': { uk: 'Вказівка: {value}', en: 'Guidance: {value}' },
  'answerKey.modelLine': { uk: 'Модельна відповідь: {value}', en: 'Model answer: {value}' },
  'answerKey.modelAnswersLine': { uk: 'Модельні відповіді: {value}', en: 'Model answers: {value}' },
  'answerKey.rubricLine': { uk: 'Рубрика: {value}', en: 'Rubric: {value}' },
  // editor buttons
  'editor.save': { uk: 'Зберегти зміни', en: 'Save changes' },
  'editor.cancel': { uk: 'Скасувати', en: 'Cancel' },
  // activity type labels (chrome labels)
  'type.true-false': { uk: 'Правда чи ні', en: 'True or false' },
  'type.cloze': { uk: 'Прогалини', en: 'Cloze' },
  'type.match-up': { uk: 'Пари', en: 'Match-up' },
  'type.quiz': { uk: 'Тест', en: 'Quiz' },
  'type.mark-the-words': { uk: 'Позначте слова', en: 'Mark the words' },
  'type.fill-in': { uk: 'Вставте слово', en: 'Fill in' },
  'type.error-correction': { uk: 'Виправте помилку', en: 'Error correction' },
  'type.text-questions': { uk: 'Питання до тексту', en: 'Text questions' },
  'type.short-writing': { uk: 'Коротке письмо', en: 'Short writing' },
  'type.flagged-notice': { uk: 'Двигун не зміг створити вправу', en: 'The engine could not create this activity' },
  // phase titles
  'phase.test1': { uk: 'Тест 1 — що учні вже знають', en: 'Test 1 — what students already know' },
  'phase.teach': { uk: 'Навчання — закриваємо прогалину', en: 'Teaching — close the gap' },
  'phase.test2': { uk: 'Тест 2 — перевіряємо ще раз', en: 'Test 2 — check again' },
  'phase.roman1': { uk: 'І.', en: 'I.' },
  'phase.roman2': { uk: 'ІІ.', en: 'II.' },
  'phase.roman3': { uk: 'ІІІ.', en: 'III.' },

  // lang toggle button labels (show the language you switch TO — demo langbtn)
  'lang.switchUk': { uk: 'УКР', en: 'УКР' },
  'lang.switchEn': { uk: 'EN', en: 'EN' },

  // baking progress / elapsed sublines (app-helpers chrome)
  'bake.step.textReceived': { uk: 'текст отримано', en: 'text received' },
  'bake.step.updating': { uk: 'оновлення…', en: 'updating…' },
  'bake.step.tasksComposed': { uk: 'завдання складено', en: 'tasks composed' },
  'bake.step.generation': { uk: 'створення завдань', en: 'creating tasks' },
  'bake.step.gates': { uk: 'перевірка', en: 'verification' },
  'bake.step.assembly': { uk: 'збирання заняття', en: 'assembling the lesson' },
  'bake.step.prep': { uk: 'приготування', en: 'preparing' },
  'bake.progressLine': { uk: 'Фаза {phase} із {total} — {step}…', en: 'Phase {phase} of {total} — {step}…' },
  'bake.providerCalls': {
    uk: ' (виклики постачальника: {count})',
    en: ' (provider calls: {count})',
  },
  'bake.elapsed.lt2': {
    uk: 'Текст отримано — складаємо завдання з вашого тексту.',
    en: 'Text received — building tasks from your text.',
  },
  'bake.elapsed.lt5': {
    uk: 'Генерація триває — це нормально. Зазвичай кілька хвилин.',
    en: 'Generation is in progress — that is normal. Usually a few minutes.',
  },
  'bake.elapsed.lt15': {
    uk: 'Ще працюємо над завданнями. Можна повернутися до списку — ми продовжимо тут.',
    en: 'Still working on the tasks. You can go back to the list — we will continue here.',
  },
  'bake.elapsed.gte15': {
    uk: 'Це може тривати до пів години. Можна повернутися до «Моїх занять» — заняття дочекається вас.',
    en: 'This can take up to half an hour. You can go back to “My lessons” — the lesson will wait for you.',
  },

  // review workbench chrome (remaining literals from #130 sweep)
  'review.docsheetMeta': {
    uk: '{level} · Тест → Навчання → Тест · ≈ {duration} хв',
    en: '{level} · Test → Teach → Test · ≈ {duration} min',
  },
  'review.phaseDuration': { uk: '≈ {minutes} хв', en: '≈ {minutes} min' },
  'review.durationChip': { uk: '{minutes} хв', en: '{minutes} min' },
  'review.rejectedNote': { uk: '(нічого не ховаємо).', en: '(nothing is hidden).' },

  // teacher feedback on engine-flagged blocks (#402)
  'feedback.prompt': { uk: 'Ваша оцінка цієї вправи:', en: 'Your verdict on this activity:' },
  'feedback.good': { uk: 'Вправа добра', en: 'Good activity' },
  'feedback.bad': { uk: 'Вправа погана', en: 'Bad activity' },
  'feedback.commentPlaceholder': { uk: 'Коментар (необовʼязково)', en: 'Comment (optional)' },
  'feedback.savedGood': { uk: 'Ви оцінили: вправа добра', en: 'Your verdict: good activity' },
  'feedback.savedBad': { uk: 'Ви оцінили: вправа погана', en: 'Your verdict: bad activity' },
  'feedback.change': { uk: 'Змінити оцінку', en: 'Change verdict' },

  // one-block regeneration (#418)
  'regeneration.open': { uk: 'Створити інший варіант', en: 'Create another version' },
  'regeneration.feedbackLabel': {
    uk: 'Що саме варто змінити? (необовʼязково)',
    en: 'What should change? (optional)',
  },
  'regeneration.feedbackPlaceholder': {
    uk: 'Наприклад: менше очевидних підказок, природніші формулювання…',
    en: 'For example: fewer obvious clues, more natural wording…',
  },
  'regeneration.submit': { uk: 'Створити новий варіант', en: 'Create new version' },
  'regeneration.queued': { uk: 'У черзі', en: 'Queued' },
  'regeneration.running': { uk: 'Створюємо новий варіант…', en: 'Creating a new version…' },
  'regeneration.keepOld': {
    uk: 'Ця вправа лишається в уроці, доки новий варіант не пройде перевірки.',
    en: 'This activity stays in the lesson until the new version passes verification.',
  },
  'regeneration.failed': { uk: 'Новий варіант не створено', en: 'New version not created' },
  'regeneration.failedFallback': {
    uk: 'Попередню вправу збережено. Спробуйте ще раз.',
    en: 'The previous activity was preserved. Please try again.',
  },
  'regeneration.retry': { uk: 'Спробувати ще раз', en: 'Try again' },
  'regeneration.succeeded': { uk: '✓ створено новий варіант', en: '✓ new version created' },

  // activity editor field labels
  'editor.field.title': { uk: 'Назва', en: 'Title' },
  'editor.field.instruction': { uk: 'Інструкція', en: 'Instruction' },
  'editor.field.statement': { uk: 'Твердження {n}', en: 'Statement {n}' },
  'editor.field.correctAnswer': { uk: 'Правильна відповідь', en: 'Correct answer' },
  'editor.option.true': { uk: 'Правда', en: 'True' },
  'editor.option.false': { uk: 'Ні', en: 'False' },
  'editor.field.clozeText': { uk: 'Текст із прогалинами', en: 'Text with gaps' },
  'editor.field.blankNumber': { uk: 'Прогалина {n} — номер', en: 'Gap {n} — number' },
  'editor.field.answer': { uk: 'Відповідь', en: 'Answer' },
  'editor.field.optionsComma': { uk: 'Варіанти (через кому)', en: 'Options (comma-separated)' },
  'editor.field.pairLeft': { uk: 'Пара {n} — ліворуч', en: 'Pair {n} — left' },
  'editor.field.pairRight': { uk: 'Пара {n} — праворуч', en: 'Pair {n} — right' },
  'editor.field.question': { uk: 'Питання {n}', en: 'Question {n}' },
  'editor.field.option': { uk: 'Варіант {letter}', en: 'Option {letter}' },
  'editor.field.correctOption': { uk: 'Правильний варіант', en: 'Correct option' },
  'editor.field.optionFallback': { uk: 'варіант {n}', en: 'option {n}' },
  'editor.field.text': { uk: 'Текст', en: 'Text' },
  'editor.field.targetWords': { uk: 'Цільові слова (через кому)', en: 'Target words (comma-separated)' },
  'editor.field.criterion': { uk: 'Критерій', en: 'Criterion' },
  'editor.field.sentence': { uk: 'Речення {n}', en: 'Sentence {n}' },
  'editor.field.errorSentence': { uk: 'Речення з помилкою {n}', en: 'Sentence with error {n}' },
  'editor.field.correctedSentence': { uk: 'Виправлене речення', en: 'Corrected sentence' },
  'editor.field.textRef': { uk: 'Посилання на текст', en: 'Reference to text' },
  'editor.field.modelAnswer': { uk: 'Модельна відповідь', en: 'Model answer' },
  'editor.field.prompt': { uk: 'Завдання', en: 'Task' },
  'editor.field.teacherGuidance': { uk: 'Вказівка для вчителя', en: 'Guidance for teacher' },
  'editor.field.rubric': { uk: 'Рубрика', en: 'Rubric' },

  // validation error labels (editor chrome)
  'editor.valLabel.title': { uk: 'назва', en: 'title' },
  'editor.valLabel.instruction': { uk: 'інструкція', en: 'instruction' },
  'editor.valLabel.text': { uk: 'текст', en: 'text' },
  'editor.valLabel.prompt': { uk: 'завдання', en: 'prompt' },
  'editor.valLabel.taskDefault': { uk: 'завдання', en: 'task' },
  'err.unsupportedType': {
    uk: 'Тип {type} не підтримується пілотом.',
    en: 'Type {type} is not supported in the pilot.',
  },
  'err.schemaDefault': { uk: 'не відповідає схемі', en: 'does not match the schema' },
  'err.schemaPath': { uk: '{path} {message}', en: '{path} {message}' },
  'err.blankField': {
    uk: 'Поле «{label}» не може складатися лише з пробілів.',
    en: 'Field «{label}» cannot consist only of spaces.',
  },

  // conductor profile stub rows (demo COND_PROFILE)
  'cond.prof1.k': { uk: 'Засвоїв', en: 'Mastered' },
  'cond.prof1.v': {
    uk: 'вищий ступінь із «ніж» (тепліший, ніж…)',
    en: 'comparative with «ніж» (тепліший, ніж…)',
  },
  'cond.prof2.k': { uk: 'Часта помилка', en: 'Common error' },
  'cond.prof2.v': {
    uk: '«дешевіша» замість «дешевша»; «сама краща» замість «найкраща»',
    en: '«дешевіша» for «дешевша»; «сама краща» for «найкраща»',
  },
  'cond.prof3.k': { uk: 'Вагається', en: 'Hesitates on' },
  'cond.prof3.v': { uk: 'найвищий ступінь (най-)', en: 'superlative (най-)' },
  'cond.prof4.k': { uk: 'Успішно виправив', en: 'Successfully fixed' },
  'cond.prof4.v': { uk: '«на поверху» → «на поверсі»', en: '«на поверху» → «на поверсі»' },
} satisfies Record<string, Entry>;

export type ChromeKey = keyof typeof DICT;
type Params = Record<string, string | number>;

function interpolate(str: string, params?: Params): string {
  if (!params) return str;
  return str.replace(/\{(\w+)\}/g, (_m, k: string) => (k in params ? String(params[k]) : `{${k}}`));
}

/** Pure translate — resolves `key` in `lang`, falls back to uk then to the key itself. */
export function translate(lang: Lang, key: ChromeKey, params?: Params): string {
  const entry = DICT[key] as Entry | undefined;
  if (!entry) return interpolate(key, params);
  return interpolate(entry[lang] ?? entry.uk, params);
}

/** UA status chip label → dictionary key (chip label is chrome; the chip class stays data-driven). */
export function statusKey(status: 'draft' | 'baking' | 'ready' | 'failed'): ChromeKey {
  switch (status) {
    case 'baking':
      return 'status.baking';
    case 'ready':
      return 'status.ready';
    case 'failed':
      return 'status.failed';
    default:
      return 'status.draft';
  }
}

/**
 * Failure-card body key for a durable bake `failure_code` (#185).
 * Unknown / null codes fall back to the generic overload copy (`recovery.body`).
 */
export function recoveryBodyKey(failureCode: string | null | undefined): ChromeKey {
  switch (failureCode) {
    case 'insufficient_anchor_capacity':
      return 'recovery.body.insufficient_anchor_capacity';
    case 'lesson_floor_unmet':
      return 'recovery.body.lesson_floor_unmet';
    case 'engine_unavailable':
      return 'recovery.body.engine_unavailable';
    case 'provider_unavailable':
      return 'recovery.body.provider_unavailable';
    case 'bake_timeout':
      return 'recovery.body.bake_timeout';
    case 'lesson_schema_invalid':
      return 'recovery.body.lesson_schema_invalid';
    case 'worker_restarted':
      return 'recovery.body.worker_restarted';
    case 'unknown_safe_failure':
      return 'recovery.body.unknown_safe_failure';
    case 'generation_failed':
      return 'recovery.body.generation_failed';
    case 'no_eligible_activities':
      return 'recovery.body.no_eligible_activities';
    default:
      return 'recovery.body';
  }
}

export type TFn = (key: ChromeKey, params?: Params) => string;

interface LangValue {
  lang: Lang;
  setLang: (l: Lang) => void;
  toggleLang: () => void;
  /** Session-boundary reset: default UA + clear persisted choice. */
  resetLang: () => void;
  t: TFn;
}

const LangContext = createContext<LangValue | null>(null);

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => loadLang());

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    saveLang(l);
  }, []);

  const toggleLang = useCallback(() => {
    setLangState((prev) => {
      const next: Lang = prev === 'en' ? 'uk' : 'en';
      saveLang(next);
      return next;
    });
  }, []);

  const resetLang = useCallback(() => {
    setLangState('uk');
    clearLang();
  }, []);

  // Keep browser-level chrome in the chosen language for assistive technology.
  useEffect(() => {
    document.title = translate(lang, 'doc.title');
    document.documentElement.lang = lang;
  }, [lang]);

  const value = useMemo<LangValue>(
    () => ({
      lang,
      setLang,
      toggleLang,
      resetLang,
      t: (key, params) => translate(lang, key, params),
    }),
    [lang, setLang, toggleLang, resetLang],
  );

  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useT(): LangValue {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error('useT must be used within a LangProvider');
  return ctx;
}
