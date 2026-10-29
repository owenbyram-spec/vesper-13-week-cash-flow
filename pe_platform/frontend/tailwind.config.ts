import type { Config } from "tailwindcss";
import typography from "@tailwindcss/typography";

const config: Config = {
  darkMode: 'class',
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        // Inter — the workspace typeface; --font-inter is set by next/font in app/layout.tsx
        sans: ['var(--font-inter)', 'Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif'],
      },
      colors: {
        // Tokens use rgb(<triplet> / <alpha-value>) so opacity modifiers (bg-primary/10) work in both themes.
        background: 'rgb(var(--color-background-rgb) / <alpha-value>)',
        surface: 'rgb(var(--color-surface-rgb) / <alpha-value>)',
        surfaceAlt: 'rgb(var(--color-surface-alt-rgb) / <alpha-value>)',
        primary: 'rgb(var(--color-primary-rgb) / <alpha-value>)',
        // primaryText: the TEXT-role variant of primary (see globals.css comment) — use for
        // text-primaryText wherever `primary` is rendered AS TEXT on a surface, not as a button fill.
        primaryText: 'rgb(var(--color-primary-text-rgb) / <alpha-value>)',
        primarySoft: 'rgb(var(--color-primary-soft-rgb) / <alpha-value>)',
        text: 'rgb(var(--color-text-rgb) / <alpha-value>)',
        textMuted: 'rgb(var(--color-text-muted-rgb) / <alpha-value>)',
        border: 'rgb(var(--color-border-rgb) / <alpha-value>)',
        danger: 'rgb(var(--color-danger-rgb) / <alpha-value>)',
        // dangerFill: the FILL-role variant of danger (see globals.css comment) — use for
        // bg-dangerFill wherever white text sits ON a danger fill (destructive buttons/cells);
        // `danger` stays the text role (text-danger on a surface). Mirrors primary/primaryText.
        dangerFill: 'rgb(var(--color-danger-fill-rgb) / <alpha-value>)',
        success: 'rgb(var(--color-success-rgb) / <alpha-value>)',
        successText: 'rgb(var(--color-success-text-rgb) / <alpha-value>)',
        // Deep end of the brand emerald gradient (landing-page CTA style)
        successDeep: 'rgb(var(--color-success-deep-rgb) / <alpha-value>)',
        warning: 'rgb(var(--color-warning-rgb) / <alpha-value>)',
        warningText: 'rgb(var(--color-warning-text-rgb) / <alpha-value>)',
        sectionHeader: 'var(--color-section-header)',
        // Theme-aware accent colors
        accentEmerald: 'rgb(var(--color-accent-emerald-rgb) / <alpha-value>)',
        accentAmber: 'rgb(var(--color-accent-amber-rgb) / <alpha-value>)',
        accentIndigo: 'rgb(var(--color-accent-indigo-rgb) / <alpha-value>)',
        accentPurple: 'rgb(var(--color-accent-purple-rgb) / <alpha-value>)',
        accentCyan: 'rgb(var(--color-accent-cyan-rgb) / <alpha-value>)',
        accentRose: 'rgb(var(--color-accent-rose-rgb) / <alpha-value>)',
        // KPI card accent surfaces (bg + border pairs; var() colors can't take /opacity)
        primaryBg: 'var(--color-primary-bg)',
        primaryBorder: 'var(--color-primary-border)',
        dangerBg: 'var(--color-danger-bg)',
        dangerBorder: 'var(--color-danger-border)',
        accentEmeraldBg: 'var(--color-accent-emerald-bg)',
        accentAmberBg: 'var(--color-accent-amber-bg)',
        accentIndigoBg: 'var(--color-accent-indigo-bg)',
        accentPurpleBg: 'var(--color-accent-purple-bg)',
        accentEmeraldBorder: 'var(--color-accent-emerald-border)',
        accentAmberBorder: 'var(--color-accent-amber-border)',
        accentIndigoBorder: 'var(--color-accent-indigo-border)',
        accentPurpleBorder: 'var(--color-accent-purple-border)',
        // Chart
        chartGrid: 'var(--color-chart-grid)',
        chartAxis: 'var(--color-chart-axis)',
        chartTooltipBg: 'var(--color-chart-tooltip-bg)',
        chartTooltipBorder: 'var(--color-chart-tooltip-border)',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        'card-hover': 'var(--shadow-card-hover)',
      },
    },
  },
  plugins: [
    typography,
  ],
};
export default config;
