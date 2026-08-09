import { createBrowserClient } from "@supabase/ssr";

// One shared browser client — used by client components (login form, the
// AppShell's sign-out button) to talk to Supabase Auth directly. Only
// handles authentication; none of the app's actual data (projects, houses,
// pricing, etc.) goes through Supabase — that's still the FastAPI/SQLite
// backend in lib/api.ts.
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!
  );
}
