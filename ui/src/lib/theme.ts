import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

// One shared store so every useTheme() caller (shell menu, command palette, charts, editors) stays in sync.
const listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function setTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("satchel.theme", theme);
  } catch {
    /* ignore */
  }
  listeners.forEach((fn) => fn());
}

export function toggleTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}

export function useTheme(): [Theme, () => void] {
  const theme = useSyncExternalStore(subscribe, currentTheme, () => "light" as Theme);
  return [theme, toggleTheme];
}
