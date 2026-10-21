// Per-category trends for the last 12 months (PRD §7.7; slice 21), two views (user, 2026-10-21): every category in
// one chart (TrendsAll, the default) or small multiples, one chart per category (below). The choice is remembered in
// this browser.
//
// Small multiples:
// Each chart has its own vertical scale, labelled at its top: rent and coffee share no axis worth reading.
// History is short at first, so the charts are sparse but never broken: the axis starts where the history starts
// (three months at least), one month of data is a point, two or more are a line, and the running month (and a
// partly covered first month) is a hollow point that says "so far". One hue (the report's accent), 2px line, 8px
// points with a surface ring, a crosshair that snaps to the nearest month on hover or with the arrow keys, its
// reading in the chart's header (never over the line), and every value again in a table (dataviz skill).
import { type KeyboardEvent, type PointerEvent, useState } from "react";

import { type TrendCategory, type Trends as TrendsData, useTrends } from "../api/report";
import { formatPaise } from "../money";
import { compact, monthLong, monthShort, nice } from "./trendMath";
import { TrendsAll } from "./TrendsAll";
import "../styles/trends.css";

// The chart's own coordinate space (SVG user units); it scales with its panel.
const W = 240;
const H = 104;
const LEFT = 34;
const RIGHT = W - 8;
const TOP = 10;
const BOTTOM = 82;
const MIN_MONTHS = 3; // the narrowest axis: fewer months than this would draw one lonely point mid-chart

type View = "one" | "each";
const VIEW_KEY = "hisaab.report.trendsView";

function readView(): View {
  try {
    return localStorage.getItem(VIEW_KEY) === "each" ? "each" : "one";
  } catch {
    return "one";  // storage blocked: the default
  }
}

export function Trends() {
  const trends = useTrends();
  const [view, setView] = useState<View>(readView);
  if (!trends.data) return null;
  const choose = (v: View) => {
    setView(v);
    try {
      localStorage.setItem(VIEW_KEY, v);
    } catch {
      // storage blocked: this visit only
    }
  };
  const t = trends.data;
  // the first month on the axis: where the history starts, or the first month any category has data
  const known = t.categories.flatMap((c) => c.months.flatMap((v, i) => (v === null ? [] : [i])));
  const historyAt = t.history_from ? t.months.indexOf(t.history_from.slice(0, 7)) : -1;
  const first = historyAt >= 0 ? historyAt : known.length ? Math.min(...known) : 0;
  const from = Math.max(0, Math.min(first, t.months.length - MIN_MONTHS));
  return (
    <section className="sheet sheet--pad trends" aria-labelledby="trends-h">
      <div className="section-head trends__head">
        <div>
          <h2 id="trends-h">Last 12 months, by category</h2>
          <p>
            {view === "one"
              ? "One scale for every category; a chart per category shows the small ones better."
              : "Each chart has its own scale."}
            {t.history_from && ` History starts ${monthLong(t.history_from.slice(0, 7))}.`}
          </p>
        </div>
        {t.categories.length > 0 && (
          <div className="seg" role="group" aria-label="Chart layout">
            <button type="button" aria-pressed={view === "one"} onClick={() => choose("one")}>One chart</button>
            <button type="button" aria-pressed={view === "each"} onClick={() => choose("each")}>Chart per category</button>
          </div>
        )}
      </div>
      {t.categories.length === 0
        ? <p className="muted">No spending yet. Each category gets a chart once money goes out.</p>
        : view === "one"
          ? <TrendsAll t={t} from={from} />
          : <div className="trends__grid">{t.categories.map((c) => <Panel key={c.category_id ?? "none"} t={t} c={c} from={from} />)}</div>}
    </section>
  );
}

function Panel({ t, c, from }: { t: TrendsData; c: TrendCategory; from: number }) {
  const [active, setActive] = useState<number | null>(null);
  const partial = new Set(t.partial);
  const known = c.months.flatMap((v, i) => (v === null || i < from ? [] : [i]));
  const values = known.map((i) => c.months[i] as number);
  const top = nice(Math.max(0, ...values));
  const bottom = -nice(-Math.min(0, ...values));
  const span = top - bottom || 1;
  const last = t.months.length - 1;
  const x = (i: number) => LEFT + ((i - from) * (RIGHT - LEFT)) / Math.max(1, last - from);
  const y = (v: number) => BOTTOM - ((v - bottom) * (BOTTOM - TOP)) / span;
  const firstKnown = known[0];
  const full = [...known].reverse().find((i) => !partial.has(t.months[i] ?? ""));
  const headline = full ?? known.at(-1);
  const nearest = (i: number) => known.reduce((best, k) => (Math.abs(k - i) < Math.abs(best - i) ? k : best), known[0] ?? 0);

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const ux = ((e.clientX - box.left) / box.width) * W;
    setActive(nearest(from + Math.round(((ux - LEFT) / (RIGHT - LEFT)) * (last - from))));
  };
  const onKey = (e: KeyboardEvent<SVGSVGElement>) => {
    const at = active === null ? known.length - 1 : known.indexOf(active);
    const next = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: known.length - 1 }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    setActive(known[Math.max(0, Math.min(known.length - 1, next))] ?? null);
  };
  const path = known.map((i, n) => `${n === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(c.months[i] as number).toFixed(1)}`).join(" ");
  const shown = active === null ? null : { ym: t.months[active] ?? "", v: c.months[active] as number };

  return (
    <figure className="trend" aria-label={c.name}>
      <figcaption className="trend__head">
        <span className="trend__name" title={c.name}>{c.name}</span>
        {shown ? (
          <span className="trend__now trend__tip" role="status">
            <strong className="trend__value">{formatPaise(shown.v)}</strong>
            {/* the header's own short form ("Oct, so far"), so hovering changes the figure and not the line's length;
                a month name is unambiguous on a 12-month axis, and the table below has the years */}
            <span className="trend__when">{monthShort(shown.ym)}{partial.has(shown.ym) ? ", so far" : ""}</span>
          </span>
        ) : headline !== undefined && (
          <span className="trend__now">
            <span className="trend__value">{formatPaise(c.months[headline] as number)}</span>
            <span className="trend__when">
              {monthShort(t.months[headline] ?? "")}
              {partial.has(t.months[headline] ?? "") ? ", so far" : ""}
            </span>
          </span>
        )}
      </figcaption>
      <div className="trend__plot">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${c.name}, last 12 months`} tabIndex={0}
          onPointerMove={onMove} onPointerDown={onMove} onKeyDown={onKey}
          onPointerLeave={(e) => { if (e.pointerType !== "touch") setActive(null); }}  // a tap keeps its reading
          onFocus={() => setActive(known.at(-1) ?? null)} onBlur={() => setActive(null)}>
          {firstKnown !== undefined && firstKnown > from && (
            <rect className="trend__before" x={LEFT} y={TOP} width={x(firstKnown) - LEFT - 6} height={BOTTOM - TOP} />
          )}
          <line className="trend__grid" x1={LEFT} x2={RIGHT} y1={y(top)} y2={y(top)} />
          <line className="trend__zero" data-testid="trend-zero" x1={LEFT} x2={RIGHT} y1={y(0)} y2={y(0)} />
          <text className="trend__tick" x={LEFT - 4} y={y(top) + 3} textAnchor="end">{compact(top)}</text>
          <text className="trend__tick" x={LEFT - 4} y={y(0) + 3} textAnchor="end">0</text>
          {bottom < 0 && <text className="trend__tick" x={LEFT - 4} y={y(bottom) + 3} textAnchor="end">{compact(bottom)}</text>}
          {[...new Set([from, Math.round((from + last) / 2), last])].map((i) => (
            <text key={i} className="trend__tick" x={x(i)} y={H - 6}
              textAnchor={i === from ? "start" : i === last ? "end" : "middle"}>
              {monthShort(t.months[i] ?? "")}
            </text>
          ))}
          {active !== null && <line className="trend__cross" x1={x(active)} x2={x(active)} y1={TOP} y2={BOTTOM} />}
          {known.length >= 2 && <path className="trend__line" d={path} data-testid="trend-line" data-points={known.length} />}
          {known.map((i) => (
            <circle key={i} className={partial.has(t.months[i] ?? "") ? "trend__point trend__point--partial" : "trend__point"}
              cx={x(i)} cy={y(c.months[i] as number)} r={4} data-testid="trend-point"
              data-partial={partial.has(t.months[i] ?? "") ? "true" : "false"} />
          ))}
        </svg>
      </div>
      <table className="visually-hidden">
        <caption>{c.name} by month</caption>
        <tbody>
          {t.months.map((m, i) => (
            <tr key={m}>
              <th scope="row">{monthLong(m)}</th>
              <td>{c.months[i] === null ? "No data yet"
                : `${formatPaise(c.months[i] as number)}${partial.has(m) ? " (so far)" : ""}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
