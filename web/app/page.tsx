import { withAuth } from "@workos-inc/authkit-nextjs";
import { redirect } from "next/navigation";

export default async function Home() {
  const { user } = await withAuth();
  if (user) redirect("/dashboards");
  return (
    <div className="card" style={{ maxWidth: 640, margin: "40px auto" }}>
      <h1>See how AI assistants talk about your brand</h1>
      <p className="muted">
        Every 15 minutes we ask ChatGPT, Perplexity, Gemini, Google AI Mode, Copilot and Grok what buyers ask
        about payments, billing, payouts and fraud, and track which brands they name, in what order, and which
        sites they cite. Build dashboards by asking in plain language.
      </p>
      <div className="row"><a className="button" href="/sign-up">Create an account</a><a href="/sign-in">Sign in</a></div>
    </div>
  );
}
