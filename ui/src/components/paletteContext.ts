import { createContext, useContext } from "react";

/** Kept free of component imports so `ui.tsx` and `CommandPalette.tsx` can both use it without a cycle. */
export const PaletteCtx = createContext<() => void>(() => {});
/** Opens the ⌘K command palette. */
export const useCommandPalette = () => useContext(PaletteCtx);

const IS_MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
export const MOD_KEY = IS_MAC ? "⌘" : "Ctrl";
