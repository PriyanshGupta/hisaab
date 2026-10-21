// Every category in one chart (user, 2026-10-21; the default view of the report's trends): one line per category on
// one shared scale, coloured from the series palette in fixed order, with a legend that is also the readout.
//
// Past eight categories the rest fold into one grey "Everything else" line, named for what it holds: a ninth colour
// would be a guess (dataviz: hues are never generated). The legend reads the latest full month; hovering the chart
// snaps a crosshair to a month (the arrow keys too) and lights the line nearest the pointer, hovering or focusing a
// legend entry lights its line, and a click pins it (a phone's tap). The lit line is drawn on top, the rest dimmed.
// A month only partly covered (the running one) is drawn dashed: "so far". Text never wears a series colour; the
// coloured key beside it carries the identity, and every value is in a table too.
import { type KeyboardEvent, type PointerEvent, useLayoutEffect, useRef, useState } from "react";

import { type TrendCategory, type Trends as TrendsData } from "../api/report";
import { formatPaise } from "../money";
import { compact, monthLong, monthShort, nice } from "./trendMath";

const SLOTS = 8;
const NEAR_PX = 16; // how close the pointer must be to a line, at the crosshair's month, to light it

type Series = { key: string; name: string; slot: string; months: (number | null)[]; members?: string[] };

/** The first eight categories (the API orders them by total) each get a colour; any more share one grey line. */
export function seriesOf(categories: TrendCategory[]): Series[] {
  const coloured: Series[] = categories.slice(0, SLOTS).map((c, i) => ({
    key: c.category_id ?? "none", name: c.name, slot: String(i + 1), months: c.months }));
  const rest = categories.slice(SLOTS);
  if (rest.length === 0) return coloured;
  const months = (categories[0]?.months ?? []).map((_, m) =>
    rest.some((c) => c.months[m] !== null) ? rest.reduce((sum, c) => sum + (c.months[m] ?? 0), 0) : null);
  return [...coloured, { key: "__rest", name: "Everything else", slot: "rest", months, members: rest.map((c) => c.name) }];
}

/** The plot's width in CSS pixels, so the chart is drawn at its real size and its labels stay readable on a phone. */
function useWidth(fallback: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measured = Math.round(el.getBoundingClientRect().width);
    if (measured > 0) setWidth(measured);
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(([entry]) => {
      const w = Math.round(entry?.contentRect.width ?? 0);
      if (w > 0) setWidth(w);
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, []);
  return [ref, width] as const;
}

export function TrendsAll({ t, from }: { t: TrendsData; from: number }) {
  const series = seriesOf(t.categories);
  const [plotRef, W] = useWidth(640);
  const [active, setActive] = useState<number | null>(null);  // the month under the crosshair
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<string | null>(null);
  const lit = hovered ?? pinned;

  const partial = new Set(t.partial);
  const last = t.months.length - 1;
  const months = Array.from({ length: last - from + 1 }, (_, n) => from + n);
  const known = (s: Series) => months.filter((i) => s.months[i] !== null);
  const values = series.flatMap((s) => known(s).map((i) => s.months[i] as number));
  const top = nice(Math.max(0, ...values));
  const bottom = -nice(-Math.min(0, ...values));
  const span = top - bottom || 1;

  const H = W < 520 ? 220 : 280;
  const LEFT = 46;
  const RIGHT = W - 10;
  const TOP = 12;
  const BOTTOM = H - 26;
  const x = (i: number) => LEFT + ((i - from) * (RIGHT - LEFT)) / Math.max(1, last - from);
  const y = (v: number) => BOTTOM - ((v - bottom) * (BOTTOM - TOP)) / span;
  const every = Math.max(1, Math.ceil(40 / ((RIGHT - LEFT) / Math.max(1, last - from))));  // a month label every n
  const ticks = [...new Set([bottom, 0, top / 2, top].filter((v) => v >= bottom && v <= top))];

  // the readout's month: the crosshair's, else the latest month any category has fully
  const latestFull = [...months].reverse().find((i) => !partial.has(t.months[i] ?? "")
    && series.some((s) => s.months[i] !== null));
  const reading = active ?? latestFull ?? last;
  const readingYm = t.months[reading] ?? "";

  const monthAt = (clientX: number, box: DOMRect) =>
    Math.max(from, Math.min(last, from + Math.round(((clientX - box.left - LEFT) / (RIGHT - LEFT)) * (last - from))));
  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const m = monthAt(e.clientX, box);
    setActive(m);
    const py = e.clientY - box.top;
    const near = series.filter((s) => s.months[m] !== null)
      .map((s) => ({ key: s.key, d: Math.abs(y(s.months[m] as number) - py) }))
      .sort((a, b) => a.d - b.d)[0];
    setHovered(near && near.d <= NEAR_PX ? near.key : null);
  };
  const onLeave = (e: PointerEvent<SVGSVGElement>) => {
    if (e.pointerType === "touch") return;  // a tap keeps its reading
    setActive(null);
    setHovered(null);
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    const next = { ArrowLeft: reading - 1, ArrowRight: reading + 1, Home: from, End: last }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    setActive(Math.max(from, Math.min(last, next)));
  };
  const state = (key: string) => (lit === null ? "" : lit === key ? " trend-all__line--lit" : " trend-all__line--dim");

  return (
    <figure className="trend-all" aria-label="All categories">
      <div className="trend-all__plot" ref={plotRef}>
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" tabIndex={0}
          aria-label={`Every category, ${monthLong(t.months[from] ?? "")} to ${monthLong(t.months[last] ?? "")}`}
          onPointerMove={onMove} onPointerDown={onMove} onPointerLeave={onLeave} onKeyDown={onKey}
          onFocus={() => setActive(last)} onBlur={() => setActive(null)}>
          {ticks.map((v) => (
            <g key={v}>
              <line className={v === 0 ? "trend__zero" : "trend__grid"} x1={LEFT} x2={RIGHT} y1={y(v)} y2={y(v)} />
              <text className="trend__tick" x={LEFT - 6} y={y(v) + 3} textAnchor="end">{v === 0 ? "0" : compact(v)}</text>
            </g>
          ))}
          {months.filter((i) => (last - i) % every === 0).map((i) => (
            <text key={i} className="trend__tick" x={x(i)} y={H - 8}
              textAnchor={i === from ? "start" : i === last ? "end" : "middle"}>{monthShort(t.months[i] ?? "")}</text>
          ))}
          {active !== null && <line className="trend__cross" x1={x(active)} x2={x(active)} y1={TOP} y2={BOTTOM} />}
          {/* the lit line last, so it is drawn over the others */}
          {[...series].sort((a, b) => Number(a.key === lit) - Number(b.key === lit)).map((s) => {
            const pts = known(s);
            const segs = pts.slice(1).map((b, n) => ({ a: pts[n] as number, b }));
            const dashed = (g: { a: number; b: number }) =>
              partial.has(t.months[g.a] ?? "") || partial.has(t.months[g.b] ?? "");
            const solid = segs.filter((g) => !dashed(g))
              .map((g) => `M${x(g.a).toFixed(1)},${y(s.months[g.a] as number).toFixed(1)}L${x(g.b).toFixed(1)},${y(s.months[g.b] as number).toFixed(1)}`)
              .join("");
            return (
              <g key={s.key} className={`trend-all__line${state(s.key)}`} data-slot={s.slot}
                data-testid={`trend-all-line-${s.name}`}>
                {solid && <path d={solid} />}
                {segs.filter(dashed).map((g) => (
                  <path key={g.b} className="trend-all__partial" data-testid={`trend-all-partial-${s.name}`}
                    d={`M${x(g.a).toFixed(1)},${y(s.months[g.a] as number).toFixed(1)}L${x(g.b).toFixed(1)},${y(s.months[g.b] as number).toFixed(1)}`} />
                ))}
                {pts.length === 1 && <circle className="trend-all__dot" cx={x(pts[0] as number)} cy={y(s.months[pts[0] as number] as number)} r={3.5} />}
                {active !== null && s.months[active] !== null && (
                  <circle className="trend-all__dot" cx={x(active)} cy={y(s.months[active] as number)} r={4} />
                )}
              </g>
            );
          })}
        </svg>
      </div>

      <div className="trend-all__side">
        <p className="trend-all__month" data-testid="trend-all-month" aria-live="polite">
          {monthLong(readingYm)}{partial.has(readingYm) ? ", so far" : ""}
        </p>
        <ul className="trend-all__legend" aria-label="Categories">
          {series.map((s) => {
            const v = s.months[reading] ?? null;
            return (
              <li key={s.key}>
                <button type="button" className={`trend-all__entry${lit !== null && lit !== s.key ? " trend-all__entry--dim" : ""}`}
                  aria-pressed={pinned === s.key}
                  onPointerEnter={() => setHovered(s.key)} onPointerLeave={() => setHovered(null)}
                  onFocus={() => setHovered(s.key)} onBlur={() => setHovered(null)}
                  onClick={() => setPinned((p) => (p === s.key ? null : s.key))}>
                  <span className="trend-all__key" data-slot={s.slot} aria-hidden="true" />
                  <span className="trend-all__name">{s.name}</span>
                  <span className="trend-all__value">{v === null ? "No data" : formatPaise(v)}</span>
                  {s.members && <span className="trend-all__members">{s.members.join(", ")}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      </div>

      {/* the box, not the table, is hidden: a table ignores the 1px width and would push a phone's page sideways */}
      <div className="visually-hidden">
      <table>
        <caption>Every category by month</caption>
        <thead>
          <tr><th scope="col">Month</th>{series.map((s) => <th key={s.key} scope="col">{s.name}</th>)}</tr>
        </thead>
        <tbody>
          {t.months.map((m, i) => (
            <tr key={m}>
              <th scope="row">{monthLong(m)}{partial.has(m) ? " (so far)" : ""}</th>
              {series.map((s) => <td key={s.key}>{s.months[i] === null ? "No data yet" : formatPaise(s.months[i] as number)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </figure>
  );
}
