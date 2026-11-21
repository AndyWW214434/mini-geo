/**
 * The app's public origin. Behind Render's proxy the server sees requests as localhost:10000,
 * so any absolute redirect must be built from the configured public URL, not the request.
 * Derived from the WorkOS redirect URI, which is already the public callback URL.
 */
export const SITE_URL = process.env.NEXT_PUBLIC_WORKOS_REDIRECT_URI
  ? new URL(process.env.NEXT_PUBLIC_WORKOS_REDIRECT_URI).origin
  : "http://localhost:3000";
