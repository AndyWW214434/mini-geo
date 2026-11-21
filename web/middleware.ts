import { authkitMiddleware } from "@workos-inc/authkit-nextjs";

// Keeps the WorkOS session fresh on every page and API proxy call (refreshes the access
// token before it expires). Pages decide themselves whether a signed-in user is required.
export default authkitMiddleware({ signUpPaths: ["/sign-up"] });

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
