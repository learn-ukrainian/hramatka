import {
  useMemo,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from 'react';
import {
  LangContext,
  loadLang,
  saveLang,
  clearLang,
  translate,
  type Lang,
  type LangValue,
} from './i18n';

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
