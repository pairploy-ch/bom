"use client";

import { useColumnMapping } from "./ColumnMappingContext";
import { Field, Input } from "./ui/primitives";

// Exposes exactly the fields the original app's sidebar let you edit —
// the rest of ColumnMapping (H/I/J/K/L/M/Q/R formula columns) stay at their
// dataclass defaults in both apps, never surfaced as widgets.
export function ColumnMappingPanel() {
  const { columnMapping: m, setColumnMapping } = useColumnMapping();

  const set = <K extends keyof typeof m>(key: K, value: (typeof m)[K]) => setColumnMapping({ ...m, [key]: value });

  return (
    <details className="rounded-xl border border-slate-200 bg-white shadow-sm">
      <summary className="cursor-pointer select-none px-5 py-3 text-sm font-semibold text-slate-700">
        ⚙️ Excel Column Mapping
      </summary>
      <div className="border-t border-slate-100 px-5 py-4">
        <p className="mb-4 text-xs text-slate-500">
          Matches the &apos;รายการเพิ่มเติม / รายการ TBC เดิม&apos; template. Adjust only if your template&apos;s
          columns differ. Resets to these defaults each session — not saved per project.
        </p>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Field label="Start row">
            <Input
              type="number"
              min={1}
              value={m.start_row}
              onChange={(e) => set("start_row", Number(e.target.value) || 1)}
            />
          </Field>
          <Field label="Supplier col">
            <Input
              value={m.supplier_col}
              maxLength={2}
              onChange={(e) => set("supplier_col", e.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Room / item-no col">
            <Input value={m.room_col} maxLength={2} onChange={(e) => set("room_col", e.target.value.toUpperCase())} />
          </Field>
          <Field label="Item name col">
            <Input value={m.item_col} maxLength={2} onChange={(e) => set("item_col", e.target.value.toUpperCase())} />
          </Field>
          <Field label="Quantity col">
            <Input value={m.qty_col} maxLength={2} onChange={(e) => set("qty_col", e.target.value.toUpperCase())} />
          </Field>
          <Field label="Price col — สั่งผลิต">
            <Input
              value={m.custom_made_price_col}
              maxLength={2}
              onChange={(e) => set("custom_made_price_col", e.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Price col — จัดซื้อ">
            <Input
              value={m.purchased_price_col}
              maxLength={2}
              onChange={(e) => set("purchased_price_col", e.target.value.toUpperCase())}
            />
          </Field>
          <Field label="Multiplier anchor row">
            <Input
              type="number"
              min={1}
              value={m.multiplier_anchor_row}
              onChange={(e) => set("multiplier_anchor_row", Number(e.target.value) || 1)}
            />
          </Field>
        </div>
        <label className="mt-4 flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={m.generate_formulas}
            onChange={(e) => set("generate_formulas", e.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          เขียนสูตร H/I/L/M/Q/R ให้อัตโนมัติ (แนะนำให้เปิด แล้วตรวจทานหลัง export)
        </label>
      </div>
    </details>
  );
}
