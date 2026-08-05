"use client";

import Link from "next/link";
import { usePathname, useParams } from "next/navigation";
import { useProject } from "@/hooks/useProject";
import { ColumnMappingProvider } from "@/components/ColumnMappingContext";
import { ColumnMappingPanel } from "@/components/ColumnMappingPanel";
import { Badge, Spinner } from "@/components/ui/primitives";

const STEPS = [
  { href: "step-1", label: "1️⃣ Excel Template" },
  { href: "step-2", label: "2️⃣ Furniture List" },
  { href: "step-3", label: "3️⃣ Supplier Mapping" },
];

export default function ProjectLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  const pathname = usePathname();
  const project = useProject(id);

  return (
    <div className="mx-auto w-full max-w-6xl px-6 py-8">
      <div className="mb-4 flex items-center justify-between">
        <Link href="/" className="text-sm text-slate-500 hover:text-indigo-600">
          ← All projects
        </Link>
        {project.data?.updated_at && (
          <span className="text-xs text-slate-400">
            Saved · last updated {new Date(project.data.updated_at).toLocaleString()}
          </span>
        )}
      </div>

      {project.isLoading && (
        <div className="flex items-center gap-2 py-16 text-slate-500">
          <Spinner /> Loading project…
        </div>
      )}

      {project.isError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          Could not load this project. It may have been deleted.
        </div>
      )}

      {project.data && (
        <ColumnMappingProvider>
          <header className="mb-6">
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">🛋️ {project.data.name}</h1>
          </header>

          <nav className="mb-6 flex gap-1 border-b border-slate-200">
            {STEPS.map((step) => {
              const href = `/projects/${id}/${step.href}`;
              const active = pathname === href;
              return (
                <Link
                  key={step.href}
                  href={href}
                  className={
                    "border-b-2 px-4 py-2.5 text-sm font-medium transition-colors " +
                    (active
                      ? "border-indigo-600 text-indigo-600"
                      : "border-transparent text-slate-500 hover:text-slate-800")
                  }
                >
                  {step.label}
                </Link>
              );
            })}
            <div className="ml-auto flex items-center gap-2 pb-2">
              {project.data.has_template ? <Badge tone="success">Template ✓</Badge> : <Badge>No template</Badge>}
              {project.data.furniture_list.length > 0 ? (
                <Badge tone="success">{project.data.furniture_list.length} items</Badge>
              ) : (
                <Badge>No furniture list</Badge>
              )}
              {project.data.has_final_export && <Badge tone="info">Exported</Badge>}
            </div>
          </nav>

          <div className="mb-6">
            <ColumnMappingPanel />
          </div>

          {children}
        </ColumnMappingProvider>
      )}
    </div>
  );
}
