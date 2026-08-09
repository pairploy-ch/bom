"use client";

import { useQuery } from "@tanstack/react-query";
import { Avatar } from "@heroui/react";
import {
  ArrowLeft,
  Calculator,
  FileText,
  HelpCircle,
  Home,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Sofa,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useParams } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { useHouse } from "@/hooks/useHouse";
import { createClient } from "@/lib/supabase/client";
import { ProfileMenu } from "@/components/ProfileMenu";
import { cn } from "@/components/ui/primitives";

// Persistent left sidebar + slim top bar, mounted once in the root layout so
// it wraps every route. Its content is route-aware: inside a house
// (/projects/:projectId/houses/:houseId/...) it shows that house's name and
// exactly the 2 items the house-level workflow needs ("คำนวณราคา" /
// "ใบราคา"); everywhere else (the project grid, a project's house list) it
// falls back to plain app branding with a single Home link, since there's
// nothing house-specific to show yet at that level. The login page renders
// its own full-screen layout, so the shell skips itself there entirely —
// proxy.ts (the Next.js route gate) already keeps unauthenticated visitors
// on /login for every other route, this just avoids flashing sidebar chrome
// around the login form itself. "Log out" actually calls Supabase; "Help &
// Information" stays static (kept for visual parity with the reference
// mockup, not wired to anything real).
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const params = useParams<{ projectId?: string; houseId?: string }>();
  const [collapsed, setCollapsed] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  const isLoginPage = pathname === "/login";
  const inHouse = pathname.startsWith("/projects/") && !!params.projectId && !!params.houseId;
  const house = useHouse(inHouse ? params.houseId! : "");
  const project = useQuery({
    queryKey: ["project", params.projectId],
    queryFn: () => api.getProjectSummary(params.projectId!),
    enabled: inHouse,
  });

  const houseNavItems = params.projectId && params.houseId
    ? [
        { href: `/projects/${params.projectId}/houses/${params.houseId}/step-1`, label: "คำนวณราคา", icon: Calculator, match: "step-" },
        { href: `/projects/${params.projectId}/houses/${params.houseId}/quotation`, label: "ใบราคา", icon: FileText, match: "quotation" },
      ]
    : [];

  const handleSignOut = async () => {
    setSigningOut(true);
    const supabase = createClient();
    await supabase.auth.signOut();
    // Full reload so proxy.ts sees the cleared session and every
    // already-mounted query/state resets cleanly.
    window.location.href = "/login";
  };

  if (isLoginPage) return <>{children}</>;

  return (
    <div className="flex h-screen">
      <aside
        className={cn(
          "flex h-full shrink-0 flex-col overflow-y-auto border-r border-slate-200 bg-white transition-[width] duration-200",
          collapsed ? "w-20" : "w-64"
        )}
      >
        {inHouse ? (
          <div className={cn("px-4 py-5", collapsed && "px-2")}>
            {!collapsed && (
              <Link
                href={`/projects/${params.projectId}`}
                className="mb-2 flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
              >
                <ArrowLeft size={12} /> {project.data?.name ?? "…"}
              </Link>
            )}
            <div className="flex items-center gap-3">
              <Avatar size="md" className="shrink-0 bg-[var(--accent)]/10 text-[var(--accent)]">
                <Avatar.Fallback>
                  <Sofa size={18} />
                </Avatar.Fallback>
              </Avatar>
              {!collapsed && (
                <p className="min-w-0 truncate text-sm font-semibold text-slate-900">{house.data?.name ?? "…"}</p>
              )}
            </div>
          </div>
        ) : (
          <div className={cn("flex items-center gap-3 px-4 py-5", collapsed && "justify-center px-2")}>
            <Avatar size="md" className="shrink-0 bg-[var(--accent)]/10 text-[var(--accent)]">
              <Avatar.Fallback>
                <Sofa size={18} />
              </Avatar.Fallback>
            </Avatar>
            {!collapsed && (
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-slate-900">SSK The Cat Workspace</p>
              </div>
            )}
          </div>
        )}

        <nav className="flex flex-1 flex-col gap-1 px-3 py-2">
          {inHouse
            ? houseNavItems.map((item) => {
                const active = pathname.includes(`/${item.match}`);
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    title={collapsed ? item.label : undefined}
                    className={cn(
                      "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                      collapsed && "justify-center px-0",
                      active
                        ? "bg-[var(--accent)]/10 text-[var(--accent)]"
                        : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                    )}
                  >
                    <Icon size={18} className="shrink-0" />
                    {!collapsed && <span className="truncate">{item.label}</span>}
                  </Link>
                );
              })
            : (
                <Link
                  href="/"
                  title={collapsed ? "หน้าแรก" : undefined}
                  className={cn(
                    "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                    collapsed && "justify-center px-0",
                    pathname === "/"
                      ? "bg-[var(--accent)]/10 text-[var(--accent)]"
                      : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                  )}
                >
                  <Home size={18} className="shrink-0" />
                  {!collapsed && <span className="truncate">หน้าแรก</span>}
                </Link>
              )}
        </nav>

        <div className="flex flex-col gap-1 border-t border-slate-100 px-3 py-3">
          <a
            href="#"
            onClick={(e) => e.preventDefault()}
            title={collapsed ? "Help & Information" : undefined}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800",
              collapsed && "justify-center px-0"
            )}
          >
            <HelpCircle size={18} className="shrink-0" />
            {!collapsed && <span className="truncate">Help &amp; Information</span>}
          </a>
          <button
            type="button"
            onClick={handleSignOut}
            disabled={signingOut}
            title={collapsed ? "Log out" : undefined}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-left text-sm text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800 disabled:opacity-60",
              collapsed && "justify-center px-0"
            )}
          >
            <LogOut size={18} className="shrink-0" />
            {!collapsed && <span className="truncate">{signingOut ? "กำลังออกจากระบบ…" : "Log out"}</span>}
          </button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="flex shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-4 py-3">
          <button
            type="button"
            onClick={() => setCollapsed((v) => !v)}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="rounded-lg p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-900"
          >
            {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
          <ProfileMenu />
        </header>

        <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
