import { Fragment } from "react";
import type { QuotationRow } from "@/lib/types";
import { cn, Input } from "@/components/ui/primitives";

const fmt = (v: number | null | undefined) =>
  v === null || v === undefined ? "" : v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// Rounds to 2 decimals for the raw editable price inputs below — backend
// now rounds these before sending, but a saved quotation from before that
// fix (or any other future source) could still carry binary-float noise
// like 151812.93750000003, and these <input type="number"> cells render
// the raw value with no formatter in between.
const round2 = (v: number) => Math.round((v + Number.EPSILON) * 100) / 100;

const OPTION_RE = /option\s*(\d+)/i;

// An item name with no "Option N" label always counts. One that does (e.g.
// "TV Console Option 1" / "Option 2" / "Option 3" — alternate design choices
// for the same piece, client picks one) counts only as "Option 1" — mirrors
// backend/app/logic.py's _is_priced_option exactly, so the live preview
// total never disagrees with the exported PDF/Word total for the same rows.
export function isPricedOption(itemName: string): boolean {
  const m = OPTION_RE.exec(itemName);
  return !m || m[1] === "1";
}

const HEADERS = ["#", "Furniture List", "จำนวน", "10DK's work", "ประมาณการงานจัดซื้อ เบิกจ่ายตามราคาจริง", "หมายเหตุ"];
// "Furniture List" ~4x wider than its old auto-sized width, "จำนวน" as
// narrow as it can reasonably go — the rest split the remainder. Requires
// table-layout: fixed to actually take effect (see the <table> className).
const COL_WIDTHS = ["4%", "44%", "5%", "13%", "19%", "15%"];

const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";

function letterLabel(index: number): string {
  let i = index + 1;
  let s = "";
  while (i > 0) {
    const rem = (i - 1) % 26;
    s = LETTERS[rem] + s;
    i = Math.floor((i - 1) / 26);
  }
  return s;
}

// Mirrors backend's assign_quotation_labels exactly, so the label shown
// while editing (before any server round-trip) always matches what the
// exported PDF/Word doc will render: a numeric sequence for rows priced
// under "10DK's work", a lettered sequence for rows priced under the
// purchase column (including still-TBC ones), and "Client's" for rows the
// customer supplies themselves (never priced).
export function assignQuotationLabels(rows: QuotationRow[]): string[] {
  const labels: string[] = [];
  let num = 0;
  let letterIdx = 0;
  for (const row of rows) {
    if (row.is_client_owned) {
      labels.push("Client's");
    } else if (row.dk_work_price !== null && row.dk_work_price !== undefined) {
      num += 1;
      labels.push(String(num));
    } else {
      labels.push(letterLabel(letterIdx));
      letterIdx += 1;
    }
  }
  return labels;
}

interface FloorRun {
  floor: string;
  start: number;
  end: number;
  rooms: { room: string; start: number; end: number }[];
}

// Splits rows into contiguous Floor > Room runs by index range (not by
// string matching), so bulk-editing a band's floor/room label only ever
// touches the exact rows currently under that band — even if the same room
// name repeats under a different floor elsewhere in the document.
function planFloorRuns(rows: QuotationRow[]): FloorRun[] {
  const runs: FloorRun[] = [];
  let i = 0;
  while (i < rows.length) {
    const floor = rows[i].floor || "";
    let j = i;
    while (j < rows.length && (rows[j].floor || "") === floor) j++;
    const rooms: FloorRun["rooms"] = [];
    let k = i;
    while (k < j) {
      const room = rows[k].room;
      let m = k;
      while (m < j && rows[m].room === room) m++;
      rooms.push({ room, start: k, end: m });
      k = m;
    }
    runs.push({ floor, start: i, end: j, rooms });
    i = j;
  }
  return runs;
}

// Parses a price input's raw text back into QuotationRow's nullable-number
// shape — an emptied field means "no price yet" (null, shows TBC), not 0.
const parsePrice = (raw: string): number | null => (raw.trim() === "" ? null : Number(raw) || 0);

// Client-facing reduced-column preview for the "ใบเสนอราคา" page, matching
// the reference template exactly: Floor > Room two-level grouping (both
// editable at the band level, applying to the whole run of rows under that
// band), numeric-vs-lettered item numbering split by which price column is
// filled, a "Client's" toggle per row (customer-owned items — no price,
// gray band), and a Total/Vat/deposit/Grand-Total footer split into the
// 10DK's-work and purchase-at-cost columns like the reference. Rows with
// neither price yet are flagged red and show "TBC", matching the reference
// template's own convention.
export function QuotationPreviewTable({
  rows,
  onRowChange,
  onRangeChange,
  dkWorkSubtotal,
  purchaseSubtotal,
  vat,
  depositDeduction,
  grandTotal,
  grandTotalNote,
  remarks,
  hasFixedLabels,
}: {
  rows: QuotationRow[];
  onRowChange: (index: number, patch: Partial<QuotationRow>) => void;
  onRangeChange: (start: number, end: number, patch: Partial<QuotationRow>) => void;
  dkWorkSubtotal: number;
  purchaseSubtotal: number;
  vat: number;
  depositDeduction: number;
  grandTotal: number;
  grandTotalNote: string;
  remarks: string;
  // True for rows uploaded from an already-exported Excel file, where each
  // row's `label` is the item's own running number from the source file's
  // room/item-no column — reuse it as-is instead of recomputing a
  // numeric/lettered sequence (which would renumber e.g. row 7/8 in a
  // "จัดซื้อ" band to "A"/"B", disagreeing with the file).
  hasFixedLabels?: boolean;
}) {
  const labels = hasFixedLabels ? rows.map((r) => r.label) : assignQuotationLabels(rows);
  const floorRuns = planFloorRuns(rows);
  const remarkLines = remarks
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-lg border border-slate-300">
        <table className="w-full min-w-[900px] table-fixed border-collapse text-sm">
          <colgroup>
            {COL_WIDTHS.map((w, i) => (
              <col key={i} style={{ width: w }} />
            ))}
          </colgroup>
          <thead>
            <tr className="bg-slate-100 text-slate-800">
              {HEADERS.map((h) => (
                <th key={h} className="border border-slate-300 px-2 py-2 text-center font-semibold">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {floorRuns.map((run) => (
              <Fragment key={`floor-${run.start}`}>
                <tr className="bg-zinc-600 text-white">
                  <td className="border border-slate-300 p-1" colSpan={6}>
                    <input
                      value={run.floor}
                      placeholder="(ระบุชั้น เช่น 1st Floor — เว้นว่างได้ถ้าไม่มีหลายชั้น)"
                      onChange={(e) => onRangeChange(run.start, run.end, { floor: e.target.value })}
                      className="w-full bg-transparent px-1 py-0.5 text-sm font-semibold text-white placeholder:font-normal placeholder:text-zinc-300 focus:outline-none focus:ring-1 focus:ring-white/50"
                    />
                  </td>
                </tr>
                {run.rooms.map((roomRun) => (
                  <Fragment key={`room-${roomRun.start}`}>
                    <tr className="bg-slate-900 text-white">
                      <td className="border border-slate-300 p-1" colSpan={6}>
                        <input
                          value={roomRun.room}
                          onChange={(e) => onRangeChange(roomRun.start, roomRun.end, { room: e.target.value })}
                          className="w-full bg-transparent px-1 py-0.5 text-sm font-semibold text-white focus:outline-none focus:ring-1 focus:ring-white/50"
                        />
                      </td>
                    </tr>
                    {Array.from({ length: roomRun.end - roomRun.start }, (_, offset) => {
                      const i = roomRun.start + offset;
                      const r = rows[i];
                      const unpriced =
                        !r.is_client_owned && r.dk_work_price == null && r.actual_price_purchase == null;
                      // "Option 2" / "Option 3" etc. — an alternate for the
                      // same item as some "Option 1" row, priced for
                      // reference but excluded from the totals below (see
                      // isPricedOption / _is_priced_option).
                      const excludedOption = !r.is_client_owned && !isPricedOption(r.item_name);
                      return (
                        <tr
                          key={i}
                          className={cn(
                            "border-b border-slate-100",
                            r.is_client_owned ? "bg-slate-200" : unpriced ? "bg-red-50" : excludedOption && "bg-amber-50"
                          )}
                        >
                          <td className="border border-slate-300 px-2 py-1.5 text-center align-top">
                            <div>{labels[i]}</div>
                            <label className="mt-1 flex items-center justify-center gap-1 text-[10px] font-normal text-slate-500">
                              <input
                                type="checkbox"
                                checked={r.is_client_owned}
                                onChange={(e) => onRowChange(i, { is_client_owned: e.target.checked })}
                                className="h-3 w-3"
                              />
                              Client&apos;s
                            </label>
                          </td>
                          <td className="border border-slate-300 p-1 align-top">
                            <Input
                              value={r.item_name}
                              onChange={(e) => onRowChange(i, { item_name: e.target.value })}
                              className="text-left"
                            />
                            {excludedOption && (
                              <p className="mt-0.5 text-[10px] font-medium text-amber-600">
                                ตัวเลือกเสริม — ไม่รวมในยอดรวม
                              </p>
                            )}
                          </td>
                          <td className="border border-slate-300 p-1 align-top">
                            <Input
                              type="number"
                              value={r.quantity}
                              onChange={(e) => onRowChange(i, { quantity: Number(e.target.value) || 0 })}
                              className="text-right"
                            />
                          </td>
                          <td className="border border-slate-300 p-1 align-top">
                            {r.is_client_owned ? (
                              <div className="px-2 py-1.5 text-center text-slate-400">—</div>
                            ) : (
                              <Input
                                type="number"
                                value={r.dk_work_price == null ? "" : round2(r.dk_work_price)}
                                onChange={(e) => onRowChange(i, { dk_work_price: parsePrice(e.target.value) })}
                                className="text-right"
                              />
                            )}
                          </td>
                          <td className="border border-slate-300 p-1 align-top">
                            {r.is_client_owned ? (
                              <div className="px-2 py-1.5 text-center text-slate-400">—</div>
                            ) : (
                              <Input
                                type="number"
                                value={r.actual_price_purchase == null ? "" : round2(r.actual_price_purchase)}
                                placeholder={unpriced ? "TBC" : undefined}
                                onChange={(e) =>
                                  onRowChange(i, { actual_price_purchase: parsePrice(e.target.value) })
                                }
                                className={cn(
                                  "text-right",
                                  unpriced && "placeholder:font-medium placeholder:text-red-600"
                                )}
                              />
                            )}
                          </td>
                          <td className="border border-slate-300 p-1 align-top">
                            <Input
                              value={r.remark}
                              onChange={(e) => onRowChange(i, { remark: e.target.value })}
                              className="text-left"
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </Fragment>
                ))}
              </Fragment>
            ))}
            {/* Column total, pinned directly under its own column (not just
                in the summary box below) so it's unambiguous that this is
                exactly the sum of the "10DK's work" / purchase cells above
                it — same rule as the summary box (no qty multiplier,
                "Option 1 only", see quotationTotals). */}
            <tr className="bg-slate-100 font-semibold text-slate-900">
              <td className="border border-slate-300 px-2 py-1.5" colSpan={3}>
                Total
              </td>
              <td className="border border-slate-300 px-2 py-1.5 text-right tabular-nums">{fmt(dkWorkSubtotal)}</td>
              <td className="border border-slate-300 px-2 py-1.5 text-right tabular-nums">{fmt(purchaseSubtotal)}</td>
              <td className="border border-slate-300 px-2 py-1.5"></td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="ml-auto w-full max-w-sm space-y-1 text-sm">
        <div className="flex justify-between">
          <span className="text-slate-500">Total (10DK&apos;s work / งานจัดซื้อ)</span>
          <span className="tabular-nums">
            {fmt(dkWorkSubtotal)} / {fmt(purchaseSubtotal)}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Vat 7%</span>
          <span className="tabular-nums">{fmt(vat)}</span>
        </div>
        {depositDeduction > 0 && (
          <div className="flex justify-between text-red-600">
            <span>หักค่ามัดจำออกแบบ</span>
            <span className="tabular-nums">{fmt(depositDeduction)}</span>
          </div>
        )}
        <div className="flex justify-between border-t border-slate-300 pt-1 font-semibold text-slate-900">
          <span>Grand Total</span>
          <span className="tabular-nums">
            {fmt(grandTotal)} / {fmt(purchaseSubtotal)}
          </span>
        </div>
        {grandTotalNote && <p className="text-right text-[11px] text-red-600">{grandTotalNote}</p>}
      </div>

      {remarkLines.length > 0 && (
        <div className="text-sm">
          <p className="font-semibold underline">Remarks:</p>
          {remarkLines.map((line, i) => (
            <p key={i} className="font-semibold">
              - {line}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// Prices are summed as-is, NOT multiplied by quantity — quantity is
// informational only and never factors into the Total, per explicit client
// instruction. Mirrors backend/app/logic.py's _quotation_row_totals exactly.
export function quotationTotals(
  rows: QuotationRow[],
  depositDeduction: number = 0
): { dkWorkSubtotal: number; purchaseSubtotal: number; vat: number; grandTotal: number } {
  let dkWorkSubtotal = 0;
  let purchaseSubtotal = 0;
  for (const r of rows) {
    if (r.is_client_owned || !isPricedOption(r.item_name)) continue;
    if (r.dk_work_price != null) dkWorkSubtotal += r.dk_work_price;
    if (r.actual_price_purchase != null) purchaseSubtotal += r.actual_price_purchase;
  }
  const vat = dkWorkSubtotal * 0.07;
  const grandTotal = dkWorkSubtotal + vat - (depositDeduction || 0);
  return { dkWorkSubtotal, purchaseSubtotal, vat, grandTotal };
}
