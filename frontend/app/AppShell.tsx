"use client";

import { Avatar } from "@heroui/react";
import { FileText, HelpCircle, LayoutDashboard, LogOut, PanelLeftClose, PanelLeftOpen, Sofa } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { cn } from "@/components/ui/primitives";

const NAV_ITEMS = [
  { href: "/", icon: LayoutDashboard, label: "แดชบอร์ด" },
  { href: "/quotation", icon: FileText, label: "ใบเสนอราคา" },
];

const BOTTOM_ITEMS = [
  { icon: HelpCircle, label: "Help & Information" },
  { icon: LogOut, label: "Log out" },
];

// Persistent left sidebar + slim top bar, mounted once in the root layout so
// it wraps every route (project pages keep their own step-tab sub-nav
// underneath this, unrelated to global navigation). No auth in this app —
// the sidebar's "profile" block is just the app's own branding, and the
// bottom Help/Log-out rows are static (kept for visual parity with the
// reference mockup, not wired to anything real).
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="flex h-screen">
      <aside
        className={cn(
          "flex h-full shrink-0 flex-col overflow-y-auto border-r border-slate-200 bg-white transition-[width] duration-200",
          collapsed ? "w-20" : "w-64"
        )}
      >
        <div className={cn("flex items-center gap-3 px-4 py-5", collapsed && "justify-center px-2")}>
          <Avatar size="md" className="shrink-0 bg-[var(--accent)]/10 text-[var(--accent)]">
            <Avatar.Fallback>
              <Sofa size={18} />
            </Avatar.Fallback>
          </Avatar>
          {!collapsed && (
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-900">Furniture BOM</p>
              <p className="truncate text-xs text-slate-500">Price Mapping</p>
            </div>
          )}
        </div>

        <nav className="flex flex-1 flex-col gap-1 px-3 py-2">
          {NAV_ITEMS.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
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
          })}
        </nav>

        <div className="flex flex-col gap-1 border-t border-slate-100 px-3 py-3">
          {BOTTOM_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <a
                key={item.label}
                href="#"
                onClick={(e) => e.preventDefault()}
                title={collapsed ? item.label : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800",
                  collapsed && "justify-center px-0"
                )}
              >
                <Icon size={18} className="shrink-0" />
                {!collapsed && <span className="truncate">{item.label}</span>}
              </a>
            );
          })}
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
        </header>

        <main className="min-w-0 flex-1 overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
