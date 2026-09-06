"use client";

import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, GripVertical, Plus, RotateCw, Search, X } from "lucide-react";
import { useParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useHouse, houseKey } from "@/hooks/useHouse";
import type { FurnitureItem } from "@/lib/types";
import { Alert, Button, Card, CardHeader, EmptyState, Input, Select, Spinner, cn } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

const NEW_ROOM_OPTION = "__new__";
const UNSPECIFIED_LABEL = "(ไม่ระบุห้อง)";
const UNSPECIFIED_ROOM_ID = "__unspecified__";
const ROW_GRID = "grid grid-cols-[24px_28px_1fr_1fr_96px_176px_32px] items-center gap-2";

// Each row gets a client-only key (never sent to the API) so drag-and-drop
// has a stable identity independently of the room/order it's currently in —
// the flat FurnitureItem[] the backend expects has no id of its own.
interface Row {
  key: string;
  item: FurnitureItem;
}

let keySeq = 0;
const makeKey = () => `row-${++keySeq}`;

const emptyRow = (room = ""): FurnitureItem => ({ room, item_name: "", quantity: 1, verified: true, spec: "" });

const roomId = (room: string) => (room === "" ? UNSPECIFIED_ROOM_ID : room);

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
  entries: Row[];
}

// Groups rows by room for display, independent of their underlying storage
// order — so the list always reads room-by-room even before "จัดกลุ่มตามห้อง"
// is clicked, and even mid-edit.
function groupByRoom(rows: Row[]): RoomGroup[] {
  const order: string[] = [];
  const map = new Map<string, Row[]>();
  for (const row of rows) {
    if (!map.has(row.item.room)) {
      map.set(row.item.room, []);
      order.push(row.item.room);
    }
    map.get(row.item.room)!.push(row);
  }
  return order.map((room) => ({ room, entries: map.get(room)! }));
}

// Reassembles a flat row order from grouped display order — keeps each
// room's rows contiguous so group order survives the next re-group.
const flatten = (groups: RoomGroup[]): Row[] => groups.flatMap((g) => g.entries);

function promptForNewRoom(): string | null {
  const name = window.prompt("ชื่อห้องใหม่:")?.trim();
  return name ? name : null;
}

export default function Step2Page() {
  const { houseId } = useParams<{ houseId: string }>();
  const house = useHouse(houseId);
  const queryClient = useQueryClient();
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [justAddedKey, setJustAddedKey] = useState<string | null>(null);
  const initialized = useRef(false);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  );

  useEffect(() => {
    if (house.data && !initialized.current) {
      setRows(house.data.furniture_list.map((item) => ({ key: makeKey(), item })));
      initialized.current = true;
    }
  }, [house.data]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: houseKey(houseId) });

  const extract = useMutation({
    mutationFn: (file: File) => api.extractFurnitureList(houseId, file),
    onSuccess: (res) => {
      setRows(res.items.map((item) => ({ key: makeKey(), item })));
      invalidate();
      toast.success(`Extracted ${res.items.length} furniture item(s).`);
      if (res.warning) toast.warning(res.warning);
      setSelectedFile(null);
      if (fileInput.current) fileInput.current.value = "";
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to extract furniture list."),
  });

  const save = useMutation({
    mutationFn: (next: FurnitureItem[]) => api.updateFurnitureList(houseId, next),
    onSuccess: () => invalidate(),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save."),
  });

  const groupByRoomMutation = useMutation({
    mutationFn: () => api.groupFurnitureByRoom(houseId),
    onSuccess: (grouped) => {
      setRows(grouped.map((item) => ({ key: makeKey(), item })));
      invalidate();
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to group by room."),
  });

  const commit = (next?: Row[]) => save.mutate((next ?? rows).map((r) => r.item));

  // Local-only edit (item name / spec / quantity commit on blur, see ItemRow).
  const updateRow = (key: string, patch: Partial<FurnitureItem>) => {
    setRows((prev) => prev.map((r) => (r.key === key ? { ...r, item: { ...r.item, ...patch } } : r)));
  };

  // Edit that should save immediately (checkbox, room reassignment).
  const updateAndCommit = (key: string, patch: Partial<FurnitureItem>) => {
    const next = rows.map((r) => (r.key === key ? { ...r, item: { ...r.item, ...patch } } : r));
    setRows(next);
    commit(next);
  };

  const removeRow = (key: string) => {
    const next = rows.filter((r) => r.key !== key);
    setRows(next);
    commit(next);
  };

  const handleRoomChange = (key: string, value: string) => {
    if (value === NEW_ROOM_OPTION) {
      const name = promptForNewRoom();
      if (name === null) return;
      updateAndCommit(key, { room: name });
      return;
    }
    updateAndCommit(key, { room: value });
  };

  // Renames every row in a room group as the header input is typed —
  // committed to the server on blur, matching the other text fields here.
  const renameRoom = (oldRoom: string, newRoom: string) => {
    setRows((prev) => prev.map((r) => (r.item.room === oldRoom ? { ...r, item: { ...r.item, room: newRoom } } : r)));
  };

  const addRowToRoom = (room: string) => {
    const key = makeKey();
    setRows((prev) => [...prev, { key, item: emptyRow(room) }]);
    setJustAddedKey(key);
  };

  const handleAddNewRoom = () => {
    const name = promptForNewRoom();
    if (name === null) return;
    addRowToRoom(name);
  };

  const handleRoomDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const groups = groupByRoom(rows);
    const oldIndex = groups.findIndex((g) => roomId(g.room) === active.id);
    const newIndex = groups.findIndex((g) => roomId(g.room) === over.id);
    if (oldIndex === -1 || newIndex === -1) return;
    const next = flatten(arrayMove(groups, oldIndex, newIndex));
    setRows(next);
    commit(next);
  };

  const handleItemDragEnd = (room: string, event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const groups = groupByRoom(rows);
    const gi = groups.findIndex((g) => g.room === room);
    if (gi === -1) return;
    const entries = groups[gi].entries;
    const oldIndex = entries.findIndex((e) => e.key === active.id);
    const newIndex = entries.findIndex((e) => e.key === over.id);
    if (oldIndex === -1 || newIndex === -1) return;
    const nextGroups = groups.slice();
    nextGroups[gi] = { room, entries: arrayMove(entries, oldIndex, newIndex) };
    const next = flatten(nextGroups);
    setRows(next);
    commit(next);
  };

  if (!house.data?.has_template) {
    return (
      <Card>
        <CardHeader title="Step 2 — Upload Floor Plan / Furniture List PDF" />
        <div className="p-5">
          <Alert tone="warning">Please complete Step 1 first.</Alert>
        </div>
      </Card>
    );
  }

  const items = rows.map((r) => r.item);
  const rooms = distinctRooms(items);
  const verifiedCount = items.filter((i) => i.verified).length;
  const totalCount = items.filter((i) => i.item_name.trim()).length;
  const groups = groupByRoom(rows);

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
          description="ลากที่จุดจับด้านซ้ายเพื่อย้ายลำดับรายการ/ห้อง, แก้ชื่อห้องได้ตรงแถบหัวห้อง, ติ๊กถูก = นำไปจับคู่ราคาต่อที่ Step 3"
          right={
            <div className="flex gap-2">
              <Button variant="secondary" onClick={handleAddNewRoom}>
                <Plus size={16} /> ห้องใหม่
              </Button>
              {items.length > 0 && (
                <Button
                  variant="secondary"
                  onClick={() => groupByRoomMutation.mutate()}
                  disabled={groupByRoomMutation.isPending}
                >
                  <RotateCw size={16} /> จัดกลุ่มตามห้อง
                </Button>
              )}
            </div>
          }
        />
        <div className="space-y-4 p-5">
          {rows.length === 0 ? (
            <EmptyState>
              No furniture items yet — extract from a PDF above, or add a room using the &quot;ห้องใหม่&quot; button above.
            </EmptyState>
          ) : (
            <>
              <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleRoomDragEnd}>
                <SortableContext items={groups.map((g) => roomId(g.room))} strategy={verticalListSortingStrategy}>
                  <div className="space-y-3">
                    {groups.map((group) => (
                      <RoomBlock
                        key={roomId(group.room)}
                        group={group}
                        rooms={rooms}
                        justAddedKey={justAddedKey}
                        onAutoFocused={() => setJustAddedKey(null)}
                        onRenameRoom={renameRoom}
                        onRenameCommit={() => commit()}
                        onAddItem={addRowToRoom}
                        onUpdateRow={updateRow}
                        onCommitRow={() => commit()}
                        onRoomChangeForItem={handleRoomChange}
                        onToggleVerified={(key, verified) => updateAndCommit(key, { verified })}
                        onRemoveRow={removeRow}
                        onItemDragEnd={handleItemDragEnd}
                        sensors={sensors}
                      />
                    ))}
                  </div>
                </SortableContext>
              </DndContext>
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

interface RoomBlockProps {
  group: RoomGroup;
  rooms: string[];
  justAddedKey: string | null;
  onAutoFocused: () => void;
  onRenameRoom: (oldRoom: string, newRoom: string) => void;
  onRenameCommit: () => void;
  onAddItem: (room: string) => void;
  onUpdateRow: (key: string, patch: Partial<FurnitureItem>) => void;
  onCommitRow: () => void;
  onRoomChangeForItem: (key: string, value: string) => void;
  onToggleVerified: (key: string, verified: boolean) => void;
  onRemoveRow: (key: string) => void;
  onItemDragEnd: (room: string, event: DragEndEvent) => void;
  sensors: ReturnType<typeof useSensors>;
}

function RoomBlock({
  group,
  rooms,
  justAddedKey,
  onAutoFocused,
  onRenameRoom,
  onRenameCommit,
  onAddItem,
  onUpdateRow,
  onCommitRow,
  onRoomChangeForItem,
  onToggleVerified,
  onRemoveRow,
  onItemDragEnd,
  sensors,
}: RoomBlockProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: roomId(group.room),
  });
  const style = { transform: CSS.Transform.toString(transform), transition };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={cn("rounded-lg border border-slate-200 bg-white", isDragging && "z-10 shadow-lg")}
    >
      <div className="flex items-center gap-2 rounded-t-lg bg-slate-900 px-2 py-1.5 text-white">
        <button
          type="button"
          {...attributes}
          {...listeners}
          className="cursor-grab text-slate-400 hover:text-white active:cursor-grabbing"
          aria-label="ลากเพื่อย้ายห้อง"
        >
          <GripVertical size={16} />
        </button>
        <input
          value={group.room}
          onChange={(e) => onRenameRoom(group.room, e.target.value)}
          onBlur={onRenameCommit}
          placeholder={UNSPECIFIED_LABEL}
          className="min-w-0 flex-1 rounded bg-transparent px-1.5 py-0.5 font-medium text-white outline-none placeholder:text-slate-400 focus:bg-white/10"
        />
        <span className="shrink-0 text-xs font-normal text-slate-300">{group.entries.length} รายการ</span>
        <button
          type="button"
          onClick={() => onAddItem(group.room)}
          className="flex shrink-0 items-center gap-1 rounded-md bg-white/10 px-2 py-1 text-xs font-medium hover:bg-white/20"
        >
          <Plus size={14} /> เพิ่มรายการ
        </button>
      </div>

      <div
        className={cn(
          ROW_GRID,
          "border-b border-slate-100 px-2 pt-2 text-left text-[11px] uppercase tracking-wide text-slate-400"
        )}
      >
        <span />
        <CheckCircle2 size={12} />
        <span>Item Name / รายการ</span>
        <span>Spec (ขนาด/วัสดุ)</span>
        <span>Qty</span>
        <span>ย้ายห้อง</span>
        <span />
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={(event) => onItemDragEnd(group.room, event)}
      >
        <SortableContext items={group.entries.map((e) => e.key)} strategy={verticalListSortingStrategy}>
          <div className="px-2 pb-2">
            {group.entries.map((entry) => (
              <ItemRow
                key={entry.key}
                entry={entry}
                rooms={rooms}
                autoFocus={entry.key === justAddedKey}
                onAutoFocused={onAutoFocused}
                onUpdate={(patch) => onUpdateRow(entry.key, patch)}
                onCommit={onCommitRow}
                onToggleVerified={(verified) => onToggleVerified(entry.key, verified)}
                onRoomChange={(value) => onRoomChangeForItem(entry.key, value)}
                onRemove={() => onRemoveRow(entry.key)}
              />
            ))}
          </div>
        </SortableContext>
      </DndContext>
    </div>
  );
}

interface ItemRowProps {
  entry: Row;
  rooms: string[];
  autoFocus: boolean;
  onAutoFocused: () => void;
  onUpdate: (patch: Partial<FurnitureItem>) => void;
  onCommit: () => void;
  onToggleVerified: (verified: boolean) => void;
  onRoomChange: (value: string) => void;
  onRemove: () => void;
}

function ItemRow({
  entry,
  rooms,
  autoFocus,
  onAutoFocused,
  onUpdate,
  onCommit,
  onToggleVerified,
  onRoomChange,
  onRemove,
}: ItemRowProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: entry.key });
  const style = { transform: CSS.Transform.toString(transform), transition, opacity: isDragging ? 0.5 : 1 };
  const { item } = entry;

  return (
    <div ref={setNodeRef} style={style} className={cn(ROW_GRID, "border-b border-slate-100 py-1.5 last:border-b-0")}>
      <button
        type="button"
        {...attributes}
        {...listeners}
        className="cursor-grab text-slate-300 hover:text-slate-500 active:cursor-grabbing"
        aria-label="ลากเพื่อย้ายลำดับรายการ"
      >
        <GripVertical size={16} />
      </button>
      <input
        type="checkbox"
        checked={item.verified}
        onChange={(e) => onToggleVerified(e.target.checked)}
        className="h-4 w-4 rounded border-slate-300"
      />
      <Input
        value={item.item_name}
        onChange={(e) => onUpdate({ item_name: e.target.value })}
        onBlur={onCommit}
        autoFocus={autoFocus}
        onFocus={onAutoFocused}
      />
      <Input
        value={item.spec}
        placeholder="เช่น 180x200cm, ไม้วีเนียร์"
        onChange={(e) => onUpdate({ spec: e.target.value })}
        onBlur={onCommit}
      />
      <Input
        type="number"
        min={0}
        value={item.quantity}
        onChange={(e) => onUpdate({ quantity: Number(e.target.value) || 0 })}
        onBlur={onCommit}
      />
      <Select value={item.room} onChange={(e) => onRoomChange(e.target.value)}>
        {rooms.map((r) => (
          <option key={r || "__empty__"} value={r}>
            {r || UNSPECIFIED_LABEL}
          </option>
        ))}
        <option value={NEW_ROOM_OPTION}>+ ห้องใหม่...</option>
      </Select>
      <button
        type="button"
        onClick={onRemove}
        aria-label="Remove row"
        className="justify-self-center text-slate-400 hover:text-red-600"
      >
        <X size={16} />
      </button>
    </div>
  );
}
