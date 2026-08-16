// jsdom does not expose a usable `localStorage` in this vitest setup, but the
// persisted Zustand store (and tests that reset it) rely on one. Provide a
// Map-backed stub. Import this module before the store is imported.
const storageData = new Map<string, string>();

Object.defineProperty(globalThis, "localStorage", {
  value: {
    getItem: (key: string) => storageData.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storageData.set(key, value);
    },
    removeItem: (key: string) => {
      storageData.delete(key);
    },
    clear: () => {
      storageData.clear();
    },
    key: (index: number) => [...storageData.keys()][index] ?? null,
    get length() {
      return storageData.size;
    },
  },
  configurable: true,
});

export {};
