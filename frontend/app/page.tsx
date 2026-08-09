"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Sofa } from "lucide-react";
import Link from "next/link";
import { useRef } from "react";
import { api, ApiError } from "@/lib/api";
import { useSupabaseUser } from "@/hooks/useSupabaseUser";
import { Alert, Card, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

// Projects are a fixed, curated set (each one's downstream calculation logic
// differs enough that spinning up a new one isn't just "add a row" — see
// backend/app/logic.py's per-template column mapping) — so unlike houses,
// there's deliberately no create/delete UI here, only a way to set each
// project's logo for this grid.
export default function HomePage() {
  const toast = useToast();
  const queryClient = useQueryClient();
  const logoInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const { user } = useSupabaseUser();
  const displayName = (user?.user_metadata?.display_name as string | undefined) || user?.email || "";

  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });

  const uploadLogo = useMutation({
    mutationFn: ({ id, file }: { id: string; file: File }) => api.uploadProjectLogo(id, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success("อัปเดตโลโก้แล้ว");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to upload logo."),
  });

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-8">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">สวัสดี! {displayName}</h1>
      </header>

      {projects.isLoading && (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Spinner /> Loading projects…
        </div>
      )}
      {projects.isError && <Alert tone="danger">Could not reach the backend API. Is it running?</Alert>}
      {projects.data && projects.data.length === 0 && (
        <p className="text-sm text-slate-500">ยังไม่มีโปรเจกต์ในระบบ</p>
      )}

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
        {projects.data?.map((p) => (
          <div key={p.id} className="group relative">
            <Link href={`/projects/${p.id}`}>
              <Card className="flex aspect-square flex-col items-center justify-center gap-3 px-4 py-4 text-center transition-colors hover:border-[var(--accent)] hover:bg-[var(--accent)]/5">
                {p.has_logo ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={api.projectLogoUrl(p.id)}
                    alt={p.name}
                    className="h-16 w-16 rounded-lg object-contain"
                  />
                ) : (
                  <div className="flex h-16 w-16 items-center justify-center rounded-lg bg-slate-100 text-slate-400">
                    <Sofa size={28} />
                  </div>
                )}
                <p className="truncate text-sm font-medium text-slate-900">{p.name}</p>
              </Card>
            </Link>
            <input
              ref={(el) => {
                logoInputs.current[p.id] = el;
              }}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) uploadLogo.mutate({ id: p.id, file });
                e.target.value = "";
              }}
            />
            <button
              onClick={() => logoInputs.current[p.id]?.click()}
              disabled={uploadLogo.isPending}
              aria-label="เปลี่ยนโลโก้"
              title="เปลี่ยนโลโก้"
              className="absolute right-2 top-2 hidden rounded-md bg-white/90 p-1.5 text-slate-400 shadow hover:text-[var(--accent)] group-hover:block disabled:opacity-60"
            >
              <Pencil size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
