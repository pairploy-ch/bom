"use client";

import { CheckCircle2, FileSpreadsheet, Link2, ListChecks, RotateCw, Sofa } from "lucide-react";
import Link from "next/link";
import { usePathname, useParams, useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { useProject, projectKey } from "@/hooks/useProject";
import { ColumnMappingProvider } from "@/components/ColumnMappingContext";
import { ColumnMappingPanel } from "@/components/ColumnMappingPanel";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

const STEPS = [
  { href: "step-1", label: "Excel Template", icon: FileSpreadsheet },
  { href: "step-2", label: "Furniture List", icon: ListChecks },
  { href: "step-3", label: "Supplier Mapping", icon: Link2 },
];

export default function ProjectLayout({ children }: { children: React.ReactNode }) {
  const { id } = useParams<{ id: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const toast = useToast();
  const project = useProject(id);

  const resetProject = useMutation({
    mutationFn: () => api.resetProject(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: projectKey(id) });
      toast.success("ล้างข้อมูลโปรเจกต์นี้เรียบร้อยแล้ว — เริ่มใหม่ได้เลย");
      router.push(`/projects/${id}/step-1`);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to reset project."),
  });

  const handleReset = () => {
    const ok = window.confirm(
      `ล้างข้อมูลทั้งหมดของโปรเจกต์ "${project.data?.name}"?\n\n` +
        "จะลบเทมเพลต Excel, furniture list, ผลจับคู่ราคา, ไฟล์ PDF ที่อัปโหลดไว้ และไฟล์ export ทั้งหมด — ย้อนกลับไม่ได้ (ชื่อโปรเจกต์ยังอยู่เหมือนเดิม)"
    );
    if (ok) resetProject.mutate();
  };

  return (
    <div className="w-full px-6 py-8">
      {project.data?.updated_at && (
        <div className="mb-4 flex items-center justify-end">
          <span className="text-xs text-slate-400">
            Saved · last updated {new Date(project.data.updated_at).toLocaleString()}
          </span>
        </div>
      )}

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
          <header className="mb-6 flex items-center justify-between gap-4">
            <h1 className="flex items-center gap-2.5 text-2xl font-bold tracking-tight text-slate-900">
              <Sofa size={22} className="text-slate-500" />
              {project.data.name}
            </h1>
            <Button variant="danger" onClick={handleReset} disabled={resetProject.isPending}>
              {resetProject.isPending ? <Spinner /> : <RotateCw size={16} />}
              Reset Project
            </Button>
          </header>

          <nav className="mb-6 flex gap-1 border-b border-slate-200">
            {STEPS.map((step) => {
              const href = `/projects/${id}/${step.href}`;
              const active = pathname === href;
              const Icon = step.icon;
              return (
                <Link
                  key={step.href}
                  href={href}
                  className={
                    "flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors " +
                    (active
                      ? "border-[var(--accent)] text-[var(--accent)]"
                      : "border-transparent text-slate-500 hover:text-slate-800")
                  }
                >
                  <Icon size={16} />
                  {step.label}
                </Link>
              );
            })}
            <div className="ml-auto flex items-center gap-2 pb-2">
              {project.data.has_template ? (
                <Badge tone="success">
                  <span className="flex items-center gap-1">
                    <CheckCircle2 size={12} /> Template
                  </span>
                </Badge>
              ) : (
                <Badge>No template</Badge>
              )}
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
