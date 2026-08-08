"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Calculator, FileText, Sofa } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Alert, Button, Card, Input, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

export default function HomePage() {
  const router = useRouter();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState("");

  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });

  const createMutation = useMutation({
    mutationFn: (name: string) => api.createProject(name),
    onSuccess: (project) => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      router.push(`/projects/${project.id}`);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to create project."),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteProject(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success("Project deleted.");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to delete project."),
  });

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    createMutation.mutate(name);
  };

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-8 flex items-center gap-3">
        <Sofa className="text-slate-700" size={28} />
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Furniture BOM &amp; Price Mapping</h1>
          <p className="mt-1 text-sm text-slate-500">
            Upload a BOM template, extract a furniture list, match supplier prices — all saved server-side, so a
            refresh (or a different browser) always picks up right where you left off.
          </p>
        </div>
      </header>

      <div className="mb-8 grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Card className="border-indigo-200 bg-indigo-50/50 px-5 py-4">
          <p className="flex items-center gap-2 font-semibold text-slate-900">
            <Calculator size={16} /> เมนูคำนวณราคา
          </p>
          <p className="mt-0.5 text-xs text-slate-500">สร้าง/แก้ไขโปรเจกต์ ดึงราคา คำนวณ BOM — เนื้อหาด้านล่างนี้ทั้งหมด</p>
        </Card>
        <Link href="/quotation">
          <Card className="h-full px-5 py-4 transition-colors hover:border-indigo-300 hover:bg-indigo-50/40">
            <p className="flex items-center gap-2 font-semibold text-slate-900">
              <FileText size={16} /> เมนูใบเสนอราคา
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              ดึงราคาที่คำนวณแล้วมาตัดคอลัมน์ภายในออก แล้ว export เป็น PDF ให้ลูกค้า
            </p>
          </Card>
        </Link>
      </div>

      {health.data && !health.data.openai_configured && (
        <div className="mb-6">
          <Alert tone="warning">
            <strong>OPENAI_API_KEY</strong> is not configured on the backend. Steps 2 and 3 (AI extraction /
            price matching) will not work until it&apos;s set — see <code>backend/.env.example</code>.
          </Alert>
        </div>
      )}

      <Card className="mb-8">
        <div className="p-5">
          <h2 className="mb-3 text-sm font-semibold text-slate-700">New project</h2>
          <form onSubmit={handleCreate} className="flex gap-2">
            <Input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="e.g. Ban Rimtan Villa"
              disabled={createMutation.isPending}
            />
            <Button type="submit" disabled={createMutation.isPending || !newName.trim()}>
              {createMutation.isPending && <Spinner />}
              Create
            </Button>
          </form>
        </div>
      </Card>

      <section>
        <h2 className="mb-3 text-sm font-semibold text-slate-700">Your projects</h2>
        {projects.isLoading && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Spinner /> Loading projects…
          </div>
        )}
        {projects.isError && <Alert tone="danger">Could not reach the backend API. Is it running?</Alert>}
        {projects.data && projects.data.length === 0 && (
          <p className="text-sm text-slate-500">No projects yet — create one above to get started.</p>
        )}
        <div className="space-y-2">
          {projects.data?.map((p) => (
            <Card key={p.id} className="flex items-center justify-between px-5 py-4">
              <Link href={`/projects/${p.id}`} className="min-w-0 flex-1">
                <p className="truncate font-medium text-slate-900 hover:text-indigo-600">{p.name}</p>
                <p className="text-xs text-slate-400">Last updated {new Date(p.updated_at).toLocaleString()}</p>
              </Link>
              <Button
                variant="danger"
                onClick={() => {
                  if (confirm(`Delete project "${p.name}"? This cannot be undone.`)) {
                    deleteMutation.mutate(p.id);
                  }
                }}
              >
                Delete
              </Button>
            </Card>
          ))}
        </div>
      </section>
    </div>
  );
}
