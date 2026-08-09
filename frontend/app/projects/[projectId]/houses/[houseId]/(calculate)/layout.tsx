"use client";

import { CheckCircle2, FileSpreadsheet, Link2, ListChecks, RotateCw } from "lucide-react";
import Link from "next/link";
import { usePathname, useParams, useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { useHouse, houseKey } from "@/hooks/useHouse";
import { ColumnMappingProvider } from "@/components/ColumnMappingContext";
import { ColumnMappingPanel } from "@/components/ColumnMappingPanel";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

// Wraps only the step-1/2/3 "คำนวณราคา" flow — a Next.js route group (the
// parens don't add a URL segment), so the sibling `quotation/` route below
// this folder is NOT wrapped by this layout (it has its own header). The
// house name + back-to-project link live in the global sidebar (AppShell),
// not here, so this layout only needs the step tabs, status badges, column
// mapping panel, and the reset action.
const STEPS = [
  { href: "step-1", label: "Excel Template", icon: FileSpreadsheet },
  { href: "step-2", label: "Furniture List", icon: ListChecks },
  { href: "step-3", label: "Supplier Mapping", icon: Link2 },
];

export default function CalculateLayout({ children }: { children: React.ReactNode }) {
  const { projectId, houseId } = useParams<{ projectId: string; houseId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const toast = useToast();
  const house = useHouse(houseId);

  const resetHouse = useMutation({
    mutationFn: () => api.resetHouse(houseId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: houseKey(houseId) });
      toast.success("ล้างข้อมูลบ้านนี้เรียบร้อยแล้ว — เริ่มใหม่ได้เลย");
      router.push(`/projects/${projectId}/houses/${houseId}/step-1`);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to reset house."),
  });

  const handleReset = () => {
    const ok = window.confirm(
      `ล้างข้อมูลทั้งหมดของบ้าน "${house.data?.name}"?\n\n` +
        "จะลบเทมเพลต Excel, furniture list, ผลจับคู่ราคา, ไฟล์ PDF ที่อัปโหลดไว้ และไฟล์ export ทั้งหมด — ย้อนกลับไม่ได้ (ชื่อบ้านยังอยู่เหมือนเดิม)"
    );
    if (ok) resetHouse.mutate();
  };

  return (
    <div className="w-full px-6 py-8">
      <div className="mb-4 flex items-center justify-end gap-3">
        {house.data?.updated_at && (
          <span className="text-xs text-slate-400">
            Saved · last updated {new Date(house.data.updated_at).toLocaleString()}
          </span>
        )}
        {house.data && (
          <Button variant="danger" onClick={handleReset} disabled={resetHouse.isPending}>
            {resetHouse.isPending ? <Spinner /> : <RotateCw size={16} />}
            Reset House
          </Button>
        )}
      </div>

      {house.isLoading && (
        <div className="flex items-center gap-2 py-16 text-slate-500">
          <Spinner /> Loading house…
        </div>
      )}

      {house.isError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
          Could not load this house. It may have been deleted.
        </div>
      )}

      {house.data && (
        <ColumnMappingProvider>
          <nav className="mb-6 flex gap-1 border-b border-slate-200">
            {STEPS.map((step) => {
              const href = `/projects/${projectId}/houses/${houseId}/${step.href}`;
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
              {house.data.has_template ? (
                <Badge tone="success">
                  <span className="flex items-center gap-1">
                    <CheckCircle2 size={12} /> Template
                  </span>
                </Badge>
              ) : (
                <Badge>No template</Badge>
              )}
              {house.data.furniture_list.length > 0 ? (
                <Badge tone="success">{house.data.furniture_list.length} items</Badge>
              ) : (
                <Badge>No furniture list</Badge>
              )}
              {house.data.has_final_export && <Badge tone="info">Exported</Badge>}
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
