// Guards for the palette (tokens.css, read directly):
// - the app is light-only for now (owner's decision, 2026-10-11): beige paper, dark green ink
// - every text/background pair the UI uses meets WCAG AA, so a palette edit that
//   hurts legibility fails here rather than in someone's eyes
import css from "./tokens.css?raw";

function tokens(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)].map((m) => [m[1]!, m[2]!]));
}

const palette = tokens(css);

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

// [foreground, background, minimum ratio]. 4.5 = body text; 3 = UI component edges and focus rings.
const PAIRS: [string, string, number][] = [
  ["ink", "paper", 4.5],
  ["ink", "paper-raised", 4.5],
  ["ink-soft", "paper", 4.5],
  ["ink-muted", "paper", 4.5],
  ["ink-muted", "paper-raised", 4.5],
  ["accent", "paper", 4.5], // links
  ["margin", "paper", 4.5], // error text
  ["ink", "margin-wash", 4.5], // attention banners: ink text, red left rule (red text there is 4.39:1)
  ["on-ink", "ink", 4.5], // primary button label
  ["on-cover", "cover", 4.5], // login cover wordmark
  ["field-border", "paper-raised", 3], // input edges
  ["accent", "paper", 3], // focus ring
];

test("the app is light-only: no dark colour scheme", () => {
  expect(css).not.toMatch(/prefers-color-scheme/);
  expect(css).toMatch(/color-scheme:\s*light/);
});

test.each(PAIRS)("%s on %s is at least %s:1", (fg, bg, min) => {
  expect(palette[fg], `--${fg} missing`).toBeDefined();
  expect(palette[bg], `--${bg} missing`).toBeDefined();
  expect(contrast(palette[fg]!, palette[bg]!)).toBeGreaterThanOrEqual(min);
});
