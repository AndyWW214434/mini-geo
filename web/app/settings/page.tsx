import { withAuth } from "@workos-inc/authkit-nextjs";
import { redirect } from "next/navigation";
import { api, ApiError } from "@/lib/api";

async function invite(form: FormData) {
  "use server";
  const email = String(form.get("email") ?? "");
  try {
    await api("/orgs/invitations", { method: "POST", body: JSON.stringify({ email, role: String(form.get("role") ?? "member") }) });
  } catch (e) {
    redirect(`/settings?error=${encodeURIComponent(e instanceof ApiError ? e.message : "invite failed")}`);
  }
  redirect(`/settings?sent=${encodeURIComponent(email)}`);
}

export default async function Settings({ searchParams }: { searchParams: Promise<{ sent?: string; error?: string }> }) {
  const { organizationId, role } = await withAuth({ ensureSignedIn: true });
  if (!organizationId) redirect("/onboarding");
  const { sent, error } = await searchParams;
  return (
    <div className="card" style={{ maxWidth: 560 }}>
      <h1>Members</h1>
      {role !== "admin" ? (
        <p className="muted">Only admins can invite members.</p>
      ) : (
        <form action={invite} className="row">
          <input type="email" name="email" placeholder="teammate@company.com" required style={{ flex: 1 }} />
          <select name="role" defaultValue="member"><option value="member">Member</option><option value="admin">Admin</option></select>
          <button type="submit">Invite</button>
        </form>
      )}
      {sent && <p>Invitation sent to {sent}.</p>}
      {error && <p className="error">{error}</p>}
    </div>
  );
}
