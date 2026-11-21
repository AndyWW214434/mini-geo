import { withAuth } from "@workos-inc/authkit-nextjs";
import { NextRequest, NextResponse } from "next/server";
import { API_URL } from "@/lib/api";

// Browser -> Next.js -> FastAPI. The WorkOS access token stays server-side; FastAPI verifies it.
async function forward(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { accessToken } = await withAuth();
  if (!accessToken) return NextResponse.json({ detail: "signed out" }, { status: 401 });
  const { path } = await params;
  const url = `${API_URL}/${path.map(encodeURIComponent).join("/")}${req.nextUrl.search}`;
  const res = await fetch(url, {
    method: req.method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
    body: ["GET", "HEAD"].includes(req.method) ? undefined : await req.text(),
    cache: "no-store",
  });
  return new NextResponse(res.status === 204 ? null : await res.text(), {
    status: res.status,
    headers: { "Content-Type": res.headers.get("Content-Type") ?? "application/json" },
  });
}

export { forward as GET, forward as POST, forward as PATCH, forward as DELETE };
