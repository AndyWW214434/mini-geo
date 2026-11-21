import { withAuth } from "@workos-inc/authkit-nextjs";
import { notFound, redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { DashboardView, type Chart } from "@/components/DashboardView";

type Dashboard = { dashboard_id: string; name: string; description: string | null; charts: Chart[] };

export default async function DashboardPage({ params }: { params: Promise<{ id: string }> }) {
  const { organizationId } = await withAuth({ ensureSignedIn: true });
  if (!organizationId) redirect("/onboarding");
  const { id } = await params;
  let dashboard: Dashboard;
  try {
    dashboard = await api<Dashboard>(`/dashboards/${id}`);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 422)) notFound();
    throw e;
  }
  return <DashboardView dashboard={dashboard} />;
}
