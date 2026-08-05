"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useProject, projectKey } from "@/hooks/useProject";
import type { FurnitureItem } from "@/lib/types";
import { Alert, Button, Card, CardHeader, EmptyState, Input, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

const emptyRow = (): FurnitureItem => ({ room: "", item_name: "", quantity: 1, verified: false });

export default function Step2Page() {
  const { id } = useParams<{ id: string }>();
  const project = useProject(id);
  const queryClient = useQueryClient();
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [items, setItems] = useState<FurnitureItem[]>([]);
  const initialized = useRef(false);

  useEffect(() => {
    if (project.data && !initialized.current) {
      setItems(project.data.furniture_list);
      initialized.current = true;
    }
  }, [project.data]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: projectKey(id) });

  const extract = useMutation({
    mutationFn: (file: File) => api.extractFurnitureList(id, file),
    onSuccess: (res) => {
      setItems(res.items);
      invalidate();
      toast.success(`✅ Extracted ${res.items.length} furniture item(s).`);
      if (res.warning) toast.warning(res.warning);
      setSelectedFile(null);
      if (fileInput.current) fileInput.current.value = "";
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to extract furniture list."),
  });

  const save = useMutation({
    mutationFn: (next: FurnitureItem[]) => api.updateFurnitureList(id, next),
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save."),
  });

  const groupByRoom = useMutation({
    mutationFn: () => api.groupFurnitureByRoom(id),
    onSuccess: (grouped) => {
      setItems(grouped);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to group by room."),
  });

  const updateRow = (index: number, patch: Partial<FurnitureItem>) => {
    setItems((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  const commit = (next?: FurnitureItem[]) => save.mutate(next ?? items);

  const removeRow = (index: number) => {
    const next = items.filter((_, i) => i !== index);
    setItems(next);
    commit(next);
  };

  const addRow = () => {
    const next = [...items, emptyRow()];
    setItems(next);
  };

  if (!project.data?.has_template) {
    return (
      <Card>
        <CardHeader title="Step 2 — Upload Floor Plan / Furniture List PDF" />
        <div className="p-5">
          <Alert tone="warning">⬆️ Please complete Step 1 first.</Alert>
        </div>
      </Card>
    );
  }

  const verifiedCount = items.filter((i) => i.verified).length;
  const totalCount = items.filter((i) => i.item_name.trim()).length;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Step 2 — Upload Floor Plan / Furniture List PDF" />
        <div className="space-y-4 p-5">
          <div className="flex flex-wrap items-center gap-3">
            <input
              ref={fileInput}
              type="file"
              accept=".pdf"
              onChange={(e) => setSelectedFile(e.target.files?.[0] ?? null)}
              className="text-sm text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:text-sm file:font-medium file:text-slate-700 hover:file:bg-slate-200"
            />
            <Button
              onClick={() => selectedFile && extract.mutate(selectedFile)}
              disabled={!selectedFile || extract.isPending}
            >
              {extract.isPending && <Spinner />}
              🔍 Extract Furniture List
            </Button>
          </div>
        </div>
      </Card>

      {items.length === 0 ? (
        <EmptyState>No furniture items yet — extract from a PDF above, or add rows manually below.</EmptyState>
      ) : (
        <Card>
          <CardHeader
            title="Extracted Furniture List"
            description="ตรวจสอบและแก้ไขได้ก่อนไป Step 3 — ถ้า AI พลาดรายการไป ให้เลื่อนไปแถวล่างสุดแล้วพิมพ์เพิ่มเองได้เลย"
            right={
              <Button variant="secondary" onClick={() => groupByRoom.mutate()} disabled={groupByRoom.isPending}>
                🔄 จัดกลุ่มตามห้อง
              </Button>
            }
          />
          <div className="overflow-x-auto p-5">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="w-14 py-2">✅</th>
                  <th className="py-2 pr-3">Room / ห้อง</th>
                  <th className="py-2 pr-3">Item Name / รายการ</th>
                  <th className="w-28 py-2 pr-3">Quantity</th>
                  <th className="w-10 py-2" />
                </tr>
              </thead>
              <tbody>
                {items.map((row, i) => (
                  <tr key={i} className="border-b border-slate-100">
                    <td className="py-1.5">
                      <input
                        type="checkbox"
                        checked={row.verified}
                        onChange={(e) => {
                          updateRow(i, { verified: e.target.checked });
                          commit(items.map((r, idx) => (idx === i ? { ...r, verified: e.target.checked } : r)));
                        }}
                        className="h-4 w-4 rounded border-slate-300"
                      />
                    </td>
                    <td className="py-1.5 pr-3">
                      <Input
                        value={row.room}
                        onChange={(e) => updateRow(i, { room: e.target.value })}
                        onBlur={() => commit()}
                      />
                    </td>
                    <td className="py-1.5 pr-3">
                      <Input
                        value={row.item_name}
                        onChange={(e) => updateRow(i, { item_name: e.target.value })}
                        onBlur={() => commit()}
                      />
                    </td>
                    <td className="py-1.5 pr-3">
                      <Input
                        type="number"
                        min={0}
                        value={row.quantity}
                        onChange={(e) => updateRow(i, { quantity: Number(e.target.value) || 0 })}
                        onBlur={() => commit()}
                      />
                    </td>
                    <td className="py-1.5 text-center">
                      <button
                        onClick={() => removeRow(i)}
                        aria-label="Remove row"
                        className="text-slate-400 hover:text-red-600"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-3 flex items-center justify-between">
              <Button variant="secondary" onClick={addRow}>
                + Add row
              </Button>
              <p className="text-xs text-slate-500">
                ติ๊กถูกแล้ว {verifiedCount}/{totalCount} รายการ (ไม่บังคับก่อนไป Step 3)
              </p>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
