// Per-category trends for the last 12 months (PRD §7.7; slice 21): small multiples, one per category.
//
// Each chart has its own vertical scale, labelled at its top: rent and coffee share no axis worth reading.
// History is short at first, so the charts are sparse but never broken: months before the history are a
// quiet band with no marks, one month of data is a point, two or more are a line, and the running month
// (and a partly covered first month) is a hollow point that says "so far". One hue (the report's accent),
// 2px line, 8px points with a surface ring, a crosshair that snaps to the nearest month on hover or with
// the arrow keys, and every value again in a table (dataviz skill).
import { type KeyboardEvent, type PointerEvent, useState } from "react";

import { type TrendCategory, type Trends as TrendsData, useTrends } from "../api/report";
import { formatPaise } from "../money";
import "../styles/trends.css";

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthShort = (ym: string) => MONTH_NAMES[Number(ym.slice(5, 7)) - 1] ?? ym;
const monthLong = (ym: string) => `${monthShort(ym)} ${ym.slice(0, 4)}`;

// The chart's own coordinate space (SVG user units); it scales with its panel.
const W = 240;
const H = 104;
const LEFT = 34;
const RIGHT = W - 8;
const TOP = 10;
const BOTTOM = 82;
const STEPS = [1, 1.5, 2, 3, 4, 5, 6, 8, 10];

/** The smallest "nice" number at or above `v` (in paise): 27,000 → 30,000; 18,240 → 20,000. */
function nice(v: number): number {
  if (v <= 0) return 0;
  const power = 10 ** Math.floor(Math.log10(v));
  return (STEPS.find((s) => s * power >= v) ?? 10) * power;
}

/** ₹30K, ₹1.5K, ₹2.4L: a scale label, never a money figure the person reads as exact. */
function compact(paise: number): string {
  const rupees = Math.abs(paise) / 100;
  const sign = paise < 0 ? "−" : "";
  const [div, unit] = rupees >= 1e7 ? [1e7, "Cr"] : rupees >= 1e5 ? [1e5, "L"] : rupees >= 1e3 ? [1e3, "K"] : [1, ""];
  const n = rupees / div;
  return `${sign}₹${Number.isInteger(n) ? n : n.toFixed(1).replace(/\.0$/, "")}${unit}`;
}

export function Trends() {
  const trends = useTrends();
  if (!trends.data) return null;
  const t = trends.data;
  return (
    <section className="sheet sheet--ruled trends" aria-labelledby="trends-h">
      <div className="moved__head">
        <h2 id="trends-h">Last 12 months, by category</h2>
        <p className="muted">
          Each chart has its own scale.
          {t.history_from && ` History starts ${monthLong(t.history_from.slice(0, 7))}.`}
        </p>
      </div>
      {t.categories.length === 0
        ? <p className="muted">No spending yet. Each category gets a chart once money goes out.</p>
        : <div className="trends__grid">{t.categories.map((c) => <Panel key={c.category_id ?? "none"} t={t} c={c} />)}</div>}
    </section>
  );
}

function Panel({ t, c }: { t: TrendsData; c: TrendCategory }) {
  const [active, setActive] = useState<number | null>(null);
  const partial = new Set(t.partial);
  const known = c.months.flatMap((v, i) => (v === null ? [] : [i]));
  const values = known.map((i) => c.months[i] as number);
  const top = nice(Math.max(0, ...values));
  const bottom = -nice(-Math.min(0, ...values));
  const span = top - bottom || 1;
  const x = (i: number) => LEFT + (i * (RIGHT - LEFT)) / (t.months.length - 1);
  const y = (v: number) => BOTTOM - ((v - bottom) * (BOTTOM - TOP)) / span;
  const firstKnown = known[0];
  const full = [...known].reverse().find((i) => !partial.has(t.months[i] ?? ""));
  const headline = full ?? known.at(-1);
  const nearest = (i: number) => known.reduce((best, k) => (Math.abs(k - i) < Math.abs(best - i) ? k : best), known[0] ?? 0);

  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const ux = ((e.clientX - box.left) / box.width) * W;
    setActive(nearest(Math.round(((ux - LEFT) / (RIGHT - LEFT)) * (t.months.length - 1))));
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
        <span className="trend__name">{c.name}</span>
        {headline !== undefined && (
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
          {firstKnown !== undefined && firstKnown > 0 && (
            <rect className="trend__before" x={LEFT} y={TOP} width={x(firstKnown) - LEFT - 6} height={BOTTOM - TOP} />
          )}
          <line className="trend__grid" x1={LEFT} x2={RIGHT} y1={y(top)} y2={y(top)} />
          <line className="trend__zero" data-testid="trend-zero" x1={LEFT} x2={RIGHT} y1={y(0)} y2={y(0)} />
          <text className="trend__tick" x={LEFT - 4} y={y(top) + 3} textAnchor="end">{compact(top)}</text>
          <text className="trend__tick" x={LEFT - 4} y={y(0) + 3} textAnchor="end">0</text>
          {bottom < 0 && <text className="trend__tick" x={LEFT - 4} y={y(bottom) + 3} textAnchor="end">{compact(bottom)}</text>}
          {[0, 6, t.months.length - 1].map((i) => (
            <text key={i} className="trend__tick" x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === 6 ? "middle" : "end"}>
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
        {shown && (
          <div className="trend__tip" role="status"
            style={{ left: `${Math.min(80, Math.max(20, (x(active ?? 0) / W) * 100))}%` }}>
            <strong>{formatPaise(shown.v)}</strong>
            <span>{monthLong(shown.ym)}{partial.has(shown.ym) ? ", so far" : ""}</span>
          </div>
        )}
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
