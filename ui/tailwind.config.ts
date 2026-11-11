import type { Config } from "tailwindcss";

const c = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        backdrop: c("backdrop"),
        bg: c("bg"),
        surface: c("surface"),
        sunken: c("sunken"),
        line: c("line"),
        ink: c("ink"),
        muted: c("muted"),
        primary: c("primary"),
        "primary-ink": c("primary-ink"),
        accent: c("accent"),
        "accent-ink": c("accent-ink"),
        "accent-soft": c("accent-soft"),
        good: c("good"),
        warn: c("warn"),
        bad: c("bad"),
        info: c("info"),
        "good-soft": c("good-soft"),
        "warn-soft": c("warn-soft"),
        "bad-soft": c("bad-soft"),
        "info-soft": c("info-soft"),
      },
      fontFamily: {
        sans: ["Geist Variable", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["Geist Mono Variable", "ui-monospace", "SF Mono", "Menlo", "Consolas", "monospace"],
      },
      borderRadius: { DEFAULT: "6px", md: "8px", lg: "10px", xl: "12px", "2xl": "16px" },
      fontSize: {
        "2xs": ["11px", "16px"],
        xs: ["12px", "16px"],
        sm: ["13px", "18px"],
        base: ["14px", "20px"],
        "2xl": ["22px", "28px"],
      },
      boxShadow: {
        panel: "0 1px 2px rgb(0 0 0 / 0.04), 0 12px 40px -12px rgb(var(--shadow) / 0.18)",
        pop: "0 4px 12px -2px rgb(var(--shadow) / 0.12), 0 16px 40px -8px rgb(var(--shadow) / 0.2)",
      },
    },
  },
  plugins: [],
} satisfies Config;
