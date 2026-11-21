import "./globals.css";
import type { Metadata } from "next";
import { signOut, withAuth } from "@workos-inc/authkit-nextjs";
import { SITE_URL } from "@/lib/site";

export const metadata: Metadata = { title: "Mini GEO", description: "How AI assistants mention your brand" };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const { user, role } = await withAuth();
  return (
    <html lang="en">
      <body>
        <header className="top">
          <a className="brand" href="/">Mini GEO</a>
          <nav>
            {user && <a href="/dashboards">Dashboards</a>}
            {user && role === "admin" && <a href="/settings">Members</a>}
            <a href={`${process.env.API_URL ?? "http://localhost:8000"}/reports/stripe`} target="_blank">Stripe report</a>
          </nav>
          {user ? (
            <form action={async () => { "use server"; await signOut({ returnTo: `${SITE_URL}/` }); }} className="row">
              <span className="muted">{user.email}</span>
              <button className="ghost" type="submit">Sign out</button>
            </form>
          ) : (
            <div className="row"><a href="/sign-in">Sign in</a><a className="button" href="/sign-up">Sign up</a></div>
          )}
        </header>
        <main className="page">{children}</main>
      </body>
    </html>
  );
}
