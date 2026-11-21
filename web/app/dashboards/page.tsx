import { withAuth } from "@workos-inc/authkit-nextjs";
import { redirect } from "next/navigation";
import { api } from "@/lib/api";

type Dashboard = { dashboard_id: string; name: string; description: string | null; charts: number; updated_at: string };

async function createDashboard(form: FormData) {
  "use server";
  const d = await api<{ dashboard_id: string }>("/dashboards", {
    method: "POST",
    body: JSON.stringify({ name: String(form.get("name") ?? "Untitled") }),
  });
  redirect(`/dashboards/${d.dashboard_id}`);
}

export default async function Dashboards() {
  const { organizationId } = await withAuth({ ensureSignedIn: true });
  if (!organizationId) redirect("/onboarding");
  const dashboards = await api<Dashboard[]>("/dashboards");
  return (
    <>
      <h1>Dashboards</h1>
      <div className="card" style={{ marginBottom: 16 }}>
        <form action={createDashboard} className="row">
          <input name="name" placeholder="New dashboard name" required style={{ flex: 1 }} />
          <button type="submit">Create dashboard</button>
        </form>
      </div>
      <div className="card">
        {dashboards.length === 0 ? (
          <p className="muted">No dashboards yet. Create one, then ask the chat for charts.</p>
        ) : (
          <ul className="list">
            {dashboards.map((d) => (
              <li key={d.dashboard_id}>
                <a href={`/dashboards/${d.dashboard_id}`}>{d.name}</a>
                <span className="muted">{d.charts} charts · updated {new Date(d.updated_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
