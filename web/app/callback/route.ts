import { handleAuth } from "@workos-inc/authkit-nextjs";
import { SITE_URL } from "@/lib/site";

// baseURL: redirect to the public URL, not the container's internal host.
export const GET = handleAuth({ returnPathname: "/dashboards", baseURL: SITE_URL });
