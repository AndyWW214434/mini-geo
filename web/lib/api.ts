import "server-only";
import { withAuth } from "@workos-inc/authkit-nextjs";

export const API_URL = process.env.API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Server-side call to the FastAPI backend as the signed-in user. The token never reaches the browser. */
export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const { accessToken } = await withAuth({ ensureSignedIn: true });
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}`, ...(init.headers ?? {}) },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return (res.status === 204 ? null : await res.json()) as T;
}
