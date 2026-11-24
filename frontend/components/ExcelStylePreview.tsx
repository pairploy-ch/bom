import { Fragment } from "react";
import type { PreviewPrice, PreviewRow } from "@/lib/types";

const fmt = (v: number | undefined, decimals = 0) =>
  v === undefined || v === null ? "" : v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });

// "[Price Not Found]" is an internal flag for the review-and-edit table
// (step-3/page.tsx) — this preview is meant to mirror the final Excel
// output, so it's stripped from the displayed name here only.
const displayItemName = (name: string) => name.replace(/\s*\[Price Not Found\]\s*/g, " ").trim();

const HEADERS = [
  "Supplier",
  "#",
  "Furniture List",
  "จำนวน",
  "ต้นทุน ALT",
  "ALT+ค่า protect+ค่าขน",
  "ALT+5%+VAT7%",
  "P'May Overhead + vat7%",
  "Other +5% / Overhead + vat7%",
  "The chosen price (before discount, if any)",
  "10DK Price",
  "งานจัดซื้อ เบิกจ่ายตามราคาจริง",
  "หมายเหตุ",
];

const PRICE_COLUMNS = [
  "g_cost", "h_loading", "i_plus_vat", "j_pmay",
  "k_other", "l_chosen", "m_10dk_price", "n_actual_price",
] as const;

interface PlannedRow {
  row: PreviewRow;
  prices: PreviewPrice[];
  isNewRoom: boolean;
  itemNo: number;
  stripe: boolean;
}

// Precomputes room-header boundaries / running item numbers / stripe parity
// once (a plain reduce, no render-time mutation), then the JSX below just
// maps over the plan — avoids reassigning variables during render, which
// React's compiler-oriented lint rules flag as unsafe.
function planRows(rows: PreviewRow[]): PlannedRow[] {
  let currentRoom: string | null = null;
  let itemNo = 0;
  let stripeIdx = 0;
  return rows.map((row) => {
    const isNewRoom = row.room !== currentRoom;
    if (isNewRoom) {
      currentRoom = row.room;
      itemNo = 0;
    }
    itemNo += 1;
    stripeIdx += 1;
    return { row, prices: [row, ...(row.alternatives ?? [])], isNewRoom, itemNo, stripe: stripeIdx % 2 === 0 };
  });
}

// Mirrors the original's render_excel_style_preview() — same column set, same
// computed numbers (from POST /preview, i.e. logic.compute_price_preview),
// same visual language (dark room-header band, alternating item rows).
export function ExcelStylePreview({ rows }: { rows: PreviewRow[] }) {
  const planned = planRows(rows);

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-300">
      <table className="w-full min-w-[1300px] border-collapse text-xs">
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
          {planned.map(({ row: r, prices, isNewRoom, itemNo, stripe }, i) => (
            <Fragment key={i}>
              {isNewRoom && (
                <tr className="bg-slate-900 text-white">
                  <td className="border border-slate-300 px-2 py-1.5">Supplier</td>
                  <td className="border border-slate-300 px-2 py-1.5 text-left" colSpan={12}>
                    {r.room}
                  </td>
                </tr>
              )}
              <tr className={stripe ? "bg-white" : "bg-emerald-50"}>
                <td className="border border-slate-300 px-2 py-1.5 text-left">
                  {[...new Set(prices.map((price) => price.supplier).filter(Boolean))].join(" / ")}
                </td>
                <td className="border border-slate-300 px-2 py-1.5 text-right">{itemNo}</td>
                <td className="border border-slate-300 px-2 py-1.5 text-left">
                  {displayItemName(r.item_name)}
                  {!!r.alternatives?.length && r.order_type === "จัดซื้อ (ราคาจริง ไม่บวกกำไร)" && (
                    <span className="mt-0.5 block text-slate-500">ใช้ยอดเบิกจ่ายตามจริง</span>
                  )}
                </td>
                <td className="border border-slate-300 px-2 py-1.5 text-right tabular-nums">{fmt(r.quantity)}</td>
                <PriceCells prices={prices} />
              </tr>
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PriceCells({ prices }: { prices: PreviewPrice[] }) {
  return (
    <>
      {PRICE_COLUMNS.map((column) => {
        const amounts = prices.map((price) => price[column]).filter((amount) => amount != null);
        return (
          <td key={column} className="border border-slate-300 px-2 py-1.5 text-right tabular-nums">
            {amounts.length > 0 ? fmt(Math.max(...amounts)) : ""}
          </td>
        );
      })}
      <td className="whitespace-pre-line border border-slate-300 px-2 py-1.5 text-left text-slate-500">
        {[...new Set(prices.map((price) => price.auto_note).filter(Boolean))].join("\n")}
      </td>
    </>
  );
}

export function previewGrandTotal(rows: PreviewRow[]): number {
  return rows.reduce((sum, r) => sum + (r.line_total || 0), 0);
}
