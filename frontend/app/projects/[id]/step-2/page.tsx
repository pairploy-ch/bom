"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Plus, RotateCw, Search, X } from "lucide-react";
import { useParams } from "next/navigation";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useProject, projectKey } from "@/hooks/useProject";
import type { FurnitureItem } from "@/lib/types";
import { Alert, Button, Card, CardHeader, EmptyState, Input, Select, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

const NEW_ROOM_OPTION = "__new__";
const UNSPECIFIED_LABEL = "(ไม่ระบุห้อง)";

const emptyRow = (room = ""): FurnitureItem => ({ room, item_name: "", quantity: 1, verified: true, spec: "" });

// Distinct room names in first-appearance order (including "" if any item has no room).
function distinctRooms(items: FurnitureItem[]): string[] {
  const seen = new Set<string>();
  const order: string[] = [];
  for (const item of items) {
    if (!seen.has(item.room)) {
      seen.add(item.room);
      order.push(item.room);
    }
  }
  return order;
}

interface RoomGroup {
  room: string;
  entries: { item: FurnitureItem; index: number }[];
}

// Groups rows by room for display, independent of their underlying storage
// order — so the table always reads room-by-room even before "จัดกลุ่มตามห้อง"
// is clicked, and even mid-edit.
function groupByRoom(items: FurnitureItem[]): RoomGroup[] {
  const order: string[] = [];
  const map = new Map<string, { item: FurnitureItem; index: number }[]>();
  items.forEach((item, index) => {
    if (!map.has(item.room)) {
      map.set(item.room, []);
      order.push(item.room);
    }
    map.get(item.room)!.push({ item, index });
  });
  return order.map((room) => ({ room, entries: map.get(room)! }));
}

function promptForNewRoom(): string | null {
  const name = window.prompt("ชื่อห้องใหม่:")?.trim();
  return name ? name : null;
}

export default function Step2Page() {
  const { id } = useParams<{ id: string }>();
  const project = useProject(id);
  const queryClient = useQueryClient();
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [items, setItems] = useState<FurnitureItem[]>([]);
  const [addRoom, setAddRoom] = useState("");
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
      toast.success(`Extracted ${res.items.length} furniture item(s).`);
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

  const groupByRoomMutation = useMutation({
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

  const handleRoomChange = (index: number, value: string) => {
    if (value === NEW_ROOM_OPTION) {
      const name = promptForNewRoom();
      if (name === null) return;
      updateRow(index, { room: name });
      commit(items.map((r, i) => (i === index ? { ...r, room: name } : r)));
      return;
    }
    updateRow(index, { room: value });
    commit(items.map((r, i) => (i === index ? { ...r, room: value } : r)));
  };

  const rooms = useMemo(() => distinctRooms(items), [items]);
  const addRoomValue = addRoom || rooms[0] || "";

  const handleAddRoomChange = (value: string) => {
    if (value === NEW_ROOM_OPTION) {
      const name = promptForNewRoom();
      if (name === null) return;
      setAddRoom(name);
      return;
    }
    setAddRoom(value);
  };

  const addRow = () => {
    const next = [...items, emptyRow(addRoomValue)];
    setItems(next);
  };

  if (!project.data?.has_template) {
    return (
      <Card>
        <CardHeader title="Step 2 — Upload Floor Plan / Furniture List PDF" />
        <div className="p-5">
          <Alert tone="warning">Please complete Step 1 first.</Alert>
        </div>
      </Card>
    );
  }

  const verifiedCount = items.filter((i) => i.verified).length;
  const totalCount = items.filter((i) => i.item_name.trim()).length;
  const groups = groupByRoom(items);

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
              {extract.isPending ? <Spinner /> : <Search size={16} />}
              Extract Furniture List
            </Button>
          </div>
          <p className="text-xs text-slate-400">
            AI จะพยายามดึง &quot;สเปค&quot; (ขนาด/วัสดุ) ของแต่ละชิ้นมาด้วยถ้าระบุไว้ในเอกสาร — แก้ไขหรือเพิ่มเองได้ในตารางด้านล่าง
          </p>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Extracted Furniture List"
          description="ติ๊กถูก = นำไปจับคู่ราคาต่อที่ Step 3, ไม่ติ๊ก = ตัดรายการนั้นออกตอนไป Step 3 — ใช้ dropdown เพื่อจัดห้องให้ไม่งง"
          right={
            items.length > 0 && (
              <Button variant="secondary" onClick={() => groupByRoomMutation.mutate()} disabled={groupByRoomMutation.isPending}>
                <RotateCw size={16} /> จัดกลุ่มตามห้อง
              </Button>
            )
          }
        />
        <div className="space-y-4 p-5">
          <div className="flex flex-wrap items-end gap-3 rounded-lg border border-dashed border-slate-300 bg-slate-50 p-3">
            <div className="w-56">
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">
                เพิ่มรายการในห้อง
              </label>
              <Select value={addRoomValue} onChange={(e) => handleAddRoomChange(e.target.value)}>
                {rooms.length === 0 && <option value="">{UNSPECIFIED_LABEL}</option>}
                {rooms.map((r) => (
                  <option key={r || "__empty__"} value={r}>
                    {r || UNSPECIFIED_LABEL}
                  </option>
                ))}
                <option value={NEW_ROOM_OPTION}>+ ห้องใหม่...</option>
              </Select>
            </div>
            <Button variant="secondary" onClick={addRow}>
              <Plus size={16} /> Add row
            </Button>
          </div>

          {items.length === 0 ? (
            <EmptyState>No furniture items yet — extract from a PDF above, or add a row using the control above.</EmptyState>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[900px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                      <th className="w-14 py-2">
                        <CheckCircle2 size={14} />
                      </th>
                      <th className="w-48 py-2 pr-3">Room / ห้อง</th>
                      <th className="py-2 pr-3">Item Name / รายการ</th>
                      <th className="py-2 pr-3">Spec (ขนาด/วัสดุ)</th>
                      <th className="w-24 py-2 pr-3">Quantity</th>
                      <th className="w-10 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {groups.map((group) => (
                      <Fragment key={group.room || "__empty__"}>
                        <tr className="bg-slate-900 text-white">
                          <td className="px-2 py-1.5" colSpan={6}>
                            {group.room || UNSPECIFIED_LABEL}{" "}
                            <span className="font-normal text-slate-300">({group.entries.length} รายการ)</span>
                          </td>
                        </tr>
                        {group.entries.map(({ item: row, index: i }) => (
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
                              <Select value={row.room} onChange={(e) => handleRoomChange(i, e.target.value)}>
                                {rooms.map((r) => (
                                  <option key={r || "__empty__"} value={r}>
                                    {r || UNSPECIFIED_LABEL}
                                  </option>
                                ))}
                                <option value={NEW_ROOM_OPTION}>+ ห้องใหม่...</option>
                              </Select>
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
                                value={row.spec}
                                placeholder="เช่น 180x200cm, ไม้วีเนียร์"
                                onChange={(e) => updateRow(i, { spec: e.target.value })}
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
                                <X size={16} />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-xs text-slate-500">
                ติ๊กถูกไว้ {verifiedCount}/{totalCount} รายการ — ไม่ติ๊ก = จะถูกตัดออกตอนจับคู่ราคาที่ Step 3
              </p>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
