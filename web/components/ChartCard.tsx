"use client";

import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type Point = { x: string | null; y: number | null; n: number | null };
type Series = { key: string; label: string; points: Point[] };
type Data = { title: string; unit: "ratio" | "rank" | "count"; date_from: string; date_to: string; series: Series[];
              spec: { type: "line" | "bar" | "table" | "number"; metric: string } };

const COLORS = ["#5b4fe0", "#d9480f", "#2b8a3e", "#1c7ed6", "#e64980", "#868e96", "#f08c00", "#0b7285"];

function fmt(v: number | null | undefined, unit: Data["unit"]) {
  if (v === null || v === undefined) return "–";
  return unit === "ratio" ? `${(v * 100).toFixed(0)}%` : unit === "rank" ? v.toFixed(2) : v.toLocaleString();
}

export function ChartCard({ chart, onDelete }: { chart: { chart_id: string; title: string }; onDelete: () => void }) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/proxy/charts/${chart.chart_id}/data`)
      .then(async (r) => (r.ok ? setData(await r.json()) : setError(await r.text())))
      .catch((e) => setError(String(e)));
  }, [chart.chart_id]);

  return (
    <div className="card">
      <div className="chart-head">
        <h2>{chart.title}</h2>
        <button className="ghost" onClick={onDelete} title="Remove chart">×</button>
      </div>
      {data && <small className="muted">{data.spec.metric.replace("_", " ")} · {data.date_from} – {data.date_to}</small>}
      {error && <p className="error">{error}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && <ChartBody data={data} />}
    </div>
  );
}

function ChartBody({ data }: { data: Data }) {
  const { unit } = data;
  if (data.spec.type === "number") {
    return <div className="big">{fmt(data.series[0]?.points[0]?.y, unit)}</div>;
  }
  if (data.spec.type === "bar") {
    const rows = data.series.map((s) => ({ name: s.label, value: s.points[0]?.y ?? null }));
    return (
      <ResponsiveContainer width="100%" height={260}>
        <BarChart data={rows} layout="vertical" margin={{ left: 24 }}>
          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
          <XAxis type="number" tickFormatter={(v) => fmt(v, unit)} domain={unit === "ratio" ? [0, 1] : ["auto", "auto"]} />
          <YAxis type="category" dataKey="name" width={120} />
          <Tooltip formatter={(v) => fmt(typeof v === "number" ? v : null, unit)} />
          <Bar dataKey="value" fill={COLORS[0]} />
        </BarChart>
      </ResponsiveContainer>
    );
  }
  const days = Array.from(new Set(data.series.flatMap((s) => s.points.map((p) => p.x)))).sort();
  const rows = days.map((d) => {
    const row: Record<string, unknown> = { day: d };
    for (const s of data.series) row[s.label] = s.points.find((p) => p.x === d)?.y ?? null;
    return row;
  });
  if (data.spec.type === "table") {
    return (
      <div style={{ maxHeight: 280, overflow: "auto" }}>
        <table className="data">
          <thead><tr><th>Day</th>{data.series.map((s) => <th key={s.key}>{s.label}</th>)}</tr></thead>
          <tbody>{rows.map((r) => (
            <tr key={String(r.day)}><td>{String(r.day)}</td>{data.series.map((s) => <td key={s.key}>{fmt(r[s.label] as number, unit)}</td>)}</tr>
          ))}</tbody>
        </table>
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={rows}>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis dataKey="day" tickFormatter={(d: string) => d?.slice(5)} minTickGap={24} />
        <YAxis tickFormatter={(v) => fmt(v, unit)} domain={unit === "ratio" ? [0, 1] : ["auto", "auto"]} width={44}
               reversed={unit === "rank"} />
        <Tooltip formatter={(v) => fmt(typeof v === "number" ? v : null, unit)} />
        <Legend />
        {data.series.map((s, i) => (
          <Line key={s.key} type="monotone" dataKey={s.label} stroke={COLORS[i % COLORS.length]} dot={false} connectNulls={false} strokeWidth={2} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
