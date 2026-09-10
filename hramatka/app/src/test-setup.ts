import '@testing-library/jest-dom';

// Node 22 can expose an undefined experimental global localStorage in worker
// processes. Keep the browser-facing language tests deterministic regardless of
// that host flag.
const values = new Map<string, string>();
const testStorage: Storage = {
  get length() { return values.size; },
  clear: () => values.clear(),
  getItem: (key) => values.get(key) ?? null,
  key: (index) => [...values.keys()][index] ?? null,
  removeItem: (key) => { values.delete(key); },
  setItem: (key, value) => { values.set(key, String(value)); },
};
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: testStorage });
Object.defineProperty(window, 'localStorage', { configurable: true, value: testStorage });
