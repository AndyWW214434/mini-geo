import { switchToOrganization, withAuth } from "@workos-inc/authkit-nextjs";
import { redirect } from "next/navigation";
import { api } from "@/lib/api";

async function createOrg(form: FormData) {
  "use server";
  const name = String(form.get("name") ?? "").trim();
  const org = await api<{ org_id: string }>("/orgs", { method: "POST", body: JSON.stringify({ name }) });
  // Re-issue the session for the new organization, so its access token carries org_id.
  await switchToOrganization(org.org_id, { returnTo: "/dashboards" });
  redirect("/dashboards");
}

export default async function Onboarding() {
  const { organizationId } = await withAuth({ ensureSignedIn: true });
  if (organizationId) redirect("/dashboards");
  return (
    <div className="card" style={{ maxWidth: 520, margin: "40px auto" }}>
      <h1>Create your organization</h1>
      <p className="muted">Dashboards belong to an organization. You will be its admin and can invite teammates.</p>
      <form action={createOrg} className="row">
        <input name="name" placeholder="Acme Inc." required minLength={2} style={{ flex: 1 }} />
        <button type="submit">Create</button>
      </form>
      <p className="muted" style={{ marginTop: 16 }}>Invited to a team? Open the invitation email instead.</p>
    </div>
  );
}
