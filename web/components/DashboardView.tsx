"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ChartCard } from "./ChartCard";
import { Chat } from "./Chat";

export type Chart = { chart_id: string; title: string; spec: Record<string, unknown>; updated_at: string };

export function DashboardView({ dashboard }: { dashboard: { dashboard_id: string; name: string; charts: Chart[] } }) {
  const router = useRouter();
  const [name, setName] = useState(dashboard.name);

  async function rename() {
    if (name.trim() && name !== dashboard.name) {
      await fetch(`/api/proxy/dashboards/${dashboard.dashboard_id}`, { method: "PATCH", body: JSON.stringify({ name }) });
      router.refresh();
    }
  }

  async function remove(chartId: string) {
    await fetch(`/api/proxy/charts/${chartId}`, { method: "DELETE" });
    router.refresh();
  }

  return (
    <div className="layout">
      <section>
        <div className="row" style={{ marginBottom: 16 }}>
          <input value={name} onChange={(e) => setName(e.target.value)} onBlur={rename}
                 style={{ fontSize: 20, fontWeight: 700, flex: 1, border: "none", background: "transparent" }} />
        </div>
        {dashboard.charts.length === 0 ? (
          <div className="card muted">
            No charts yet. Try: “Show me Stripe vs Adyen visibility on ChatGPT this month as a line chart”.
          </div>
        ) : (
          <div className="grid">
            {dashboard.charts.map((c) => (
              <ChartCard key={`${c.chart_id}-${c.updated_at}`} chart={c} onDelete={() => remove(c.chart_id)} />
            ))}
          </div>
        )}
      </section>
      <Chat dashboardId={dashboard.dashboard_id} onChange={() => router.refresh()} />
    </div>
  );
}
