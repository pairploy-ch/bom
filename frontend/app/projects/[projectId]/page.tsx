"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Home, Pencil, X } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { Alert, Button, Card, Input, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

export default function ProjectHousesPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const router = useRouter();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");

  const project = useQuery({
    queryKey: ["project", projectId],
    queryFn: () => api.getProjectSummary(projectId),
    enabled: !!projectId,
  });

  const houses = useQuery({
    queryKey: ["houses", projectId],
    queryFn: () => api.listHouses(projectId),
    enabled: !!projectId,
  });

  const createMutation = useMutation({
    mutationFn: (name: string) => api.createHouse(projectId, name),
    onSuccess: (house) => {
      queryClient.invalidateQueries({ queryKey: ["houses", projectId] });
      router.push(`/projects/${projectId}/houses/${house.id}`);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to create house."),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteHouse(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["houses", projectId] });
      toast.success("House deleted.");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to delete house."),
  });

  const renameMutation = useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) => api.renameHouse(id, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["houses", projectId] });
      setEditingId(null);
      toast.success("แก้ไขชื่อบ้านแล้ว");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to rename house."),
  });

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    createMutation.mutate(name);
  };

  const startEditing = (id: string, currentName: string) => {
    setEditingId(id);
    setEditingName(currentName);
  };

  const submitRename = (id: string) => {
    const name = editingName.trim();
    if (!name) return;
    renameMutation.mutate({ id, name });
  };

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-8">
        <Link href="/" className="flex items-center gap-1 text-sm text-indigo-600 hover:underline">
          <ArrowLeft size={14} /> โปรเจกต์ทั้งหมด
        </Link>
        <h1 className="mt-2 flex items-center gap-2.5 text-2xl font-bold tracking-tight text-slate-900">
          <Home size={22} className="text-slate-500" />
          {project.data?.name ?? "…"}
        </h1>
        <p className="mt-1 text-sm text-slate-500">รายชื่อบ้านทั้งหมดในโปรเจกต์นี้</p>
      </header>

      <Card className="mb-8">
        <div className="p-5">
          <h2 className="mb-3 text-sm font-semibold text-slate-700">บ้านใหม่</h2>
          <form onSubmit={handleCreate} className="flex gap-2">
            <Input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="เช่น Ban Rimtan Villa"
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
        <h2 className="mb-3 text-sm font-semibold text-slate-700">บ้านในโปรเจกต์นี้</h2>
        {houses.isLoading && (
          <div className="flex items-center gap-2 text-sm text-slate-500">
            <Spinner /> Loading houses…
          </div>
        )}
        {houses.isError && <Alert tone="danger">Could not reach the backend API. Is it running?</Alert>}
        {houses.data && houses.data.length === 0 && (
          <p className="text-sm text-slate-500">No houses yet — create one above to get started.</p>
        )}
        <div className="space-y-2">
          {houses.data?.map((h) =>
            editingId === h.id ? (
              <Card key={h.id} className="flex flex-row items-center gap-2 px-5 py-4">
                <Input
                  autoFocus
                  value={editingName}
                  onChange={(e) => setEditingName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") submitRename(h.id);
                    if (e.key === "Escape") setEditingId(null);
                  }}
                  disabled={renameMutation.isPending}
                  className="flex-1"
                />
                <Button
                  variant="secondary"
                  onClick={() => submitRename(h.id)}
                  disabled={renameMutation.isPending || !editingName.trim()}
                >
                  {renameMutation.isPending ? <Spinner /> : <Check size={16} />}
                </Button>
                <Button variant="secondary" onClick={() => setEditingId(null)} disabled={renameMutation.isPending}>
                  <X size={16} />
                </Button>
              </Card>
            ) : (
              <Card key={h.id} className="flex flex-row items-center justify-between px-5 py-4">
                <Link href={`/projects/${projectId}/houses/${h.id}`} className="min-w-0 flex-1">
                  <p className="truncate font-medium text-slate-900 hover:text-indigo-600">{h.name}</p>
                  <p className="text-xs text-slate-400">Last updated {new Date(h.updated_at).toLocaleString()}</p>
                </Link>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    onClick={() => startEditing(h.id, h.name)}
                    aria-label="แก้ไขชื่อ"
                    title="แก้ไขชื่อ"
                    className="rounded-md p-2 text-slate-400 hover:bg-slate-100 hover:text-indigo-600"
                  >
                    <Pencil size={16} />
                  </button>
                  <Button
                    variant="danger"
                    onClick={() => {
                      if (confirm(`Delete house "${h.name}"? This cannot be undone.`)) {
                        deleteMutation.mutate(h.id);
                      }
                    }}
                  >
                    Delete
                  </Button>
                </div>
              </Card>
            )
          )}
        </div>
      </section>
    </div>
  );
}
