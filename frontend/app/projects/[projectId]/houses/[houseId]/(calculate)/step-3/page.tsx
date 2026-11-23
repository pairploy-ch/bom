"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  Clock,
  Coins,
  Download,
  Eye,
  Factory,
  FileText,
  Link2,
  Paperclip,
  Pencil,
  Ruler,
  Save,
  Search,
  Settings,
  ShoppingCart,
  X,
  XCircle,
} from "lucide-react";
import { useParams } from "next/navigation";
import { Fragment, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useHouse, houseKey } from "@/hooks/useHouse";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useColumnMapping } from "@/components/ColumnMappingContext";
import { ExcelStylePreview, previewGrandTotal } from "@/components/ExcelStylePreview";
import type { ExportVersionMeta, HouseState, MappingRow, PreviewResponse } from "@/lib/types";
import { ORDER_TYPES } from "@/lib/types";
import { Alert, Badge, Button, Card, CardHeader, Field, Input, Select, Spinner, cn } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

const emptyRow = (): MappingRow => ({
  room: "",
  item_name: "",
  quantity: 1,
  unit_price: 0,
  alt_price: 0,
  pmay_price: 0,
  other_maker_price: 0,
  supplier: "",
  order_type: "จัดซื้อ (ราคาจริง ไม่บวกกำไร)",
  spec: "",
  quotation_spec: "",
});

function formatDateTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString("th-TH", { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso;
  }
}

const NEW_ROOM_OPTION = "__new__";
const UNSPECIFIED_LABEL = "(ไม่ระบุห้อง)";

interface MappingRoomGroup {
  room: string;
  entries: { row: MappingRow; index: number }[];
}

// Groups mapping rows by room for display — same pattern as Step 2's
// furniture list, so the review grid reads section-by-section instead of one
// long flat table mixing every room together.
function groupMappingRowsByRoom(rows: MappingRow[]): MappingRoomGroup[] {
  const order: string[] = [];
  const map = new Map<string, { row: MappingRow; index: number }[]>();
  rows.forEach((row, index) => {
    if (!map.has(row.room)) {
      map.set(row.room, []);
      order.push(row.room);
    }
    map.get(row.room)!.push({ row, index });
  });
  return order.map((room) => ({ room, entries: map.get(room)! }));
}

function promptForNewRoom(): string | null {
  const name = window.prompt("ชื่อห้องใหม่:")?.trim();
  return name ? name : null;
}

function isUnpriced(row: MappingRow): boolean {
  if (row.order_type === "จัดซื้อ (บวกกำไร 10DK)") return row.other_maker_price <= 0;
  if (row.order_type === "จัดซื้อ (ราคาจริง ไม่บวกกำไร)") return row.unit_price <= 0;
  return row.unit_price <= 0 && row.alt_price <= 0 && row.pmay_price <= 0 && row.other_maker_price <= 0;
}

const BUCKETS = [
  { key: "alt", title: "ALT (สั่งผลิต)", icon: Factory, hint: "" },
  { key: "pmay", title: "P'May (สั่งผลิต)", icon: Factory, hint: "" },
  { key: "other", title: "Other (จัดซื้อ บวกกำไร 10DK)", icon: Factory, hint: "" },
  { key: "purchase", title: "เบิกจ่ายตามจริง (จัดซื้อ)", icon: ShoppingCart, hint: "ร้านทั่วไป เช่น SB, Index, IKEA" },
] as const;
type BucketKey = (typeof BUCKETS)[number]["key"];

export default function Step3Page() {
  const { houseId } = useParams<{ houseId: string }>();
  const house = useHouse(houseId);

  if (!house.data || house.data.furniture_list.length === 0) {
    return (
      <Card>
        <CardHeader title="Step 3 — Upload Supplier Quotation & Final Mapping" />
        <div className="p-5">
          <Alert tone="warning">Please complete Step 2 first.</Alert>
        </div>
      </Card>
    );
  }

  // Separate component so the local "draft" state below can be initialized
  // directly from the loaded house via lazy useState initializers — no
  // effect-based sync needed, since this only ever mounts once house data
  // already exists (the guard above), and remounts cleanly (key={houseId}) if
  // the house id changes.
  return <Step3Content key={houseId} houseId={houseId} initial={house.data} />;
}

function Step3Content({ houseId, initial }: { houseId: string; initial: HouseState }) {
  const house = useHouse(houseId);
  const queryClient = useQueryClient();
  const toast = useToast();
  const { columnMapping } = useColumnMapping();

  const [sheetName, setSheetName] = useState(initial.target_sheet_name || initial.sheet_names[0] || "");
  const [files, setFiles] = useState<Record<BucketKey, File[]>>({ alt: [], pmay: [], other: [], purchase: [] });
  const [rows, setRows] = useState<MappingRow[]>(initial.mapping_rows);
  const [rowsDirty, setRowsDirty] = useState(false);
  const [baseline, setBaseline] = useState<string>(
    initial.baseline_furniture_value != null ? String(initial.baseline_furniture_value) : ""
  );
  const [altInputs, setAltInputs] = useState({
    sum: initial.alt_batch_info?.sum_of_item_costs || 0,
    protection: initial.alt_batch_info?.protection_fee || 0,
    management: initial.alt_batch_info?.management_fee || 0,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: houseKey(houseId) });
  const refreshPriceCalculations = () => {
    queryClient.invalidateQueries({ queryKey: ["preview", houseId] });
    queryClient.invalidateQueries({ queryKey: ["export-status", houseId] });
  };

  useEffect(() => {
    if (!rowsDirty) return;
    const handler = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [rowsDirty]);

  // ---------------------------------------------------------- matching --

  const matchPrices = useMutation({
    mutationFn: () => api.matchPrices(houseId, sheetName, files),
    onSuccess: (res) => {
      setRows(res.mapping_rows);
      setRowsDirty(false);
      if (res.matched_buckets.includes("ALT")) {
        setAltTouched(false);
        setAltInputs({
          sum: res.alt_batch_info?.sum_of_item_costs ?? 0,
          protection: res.alt_batch_info?.protection_fee ?? 0,
          management: res.alt_batch_info?.management_fee ?? 0,
        });
      }
      invalidate();
      refreshPriceCalculations();
      toast.success(`Matched against ${res.matched_buckets.length} batch(es): ${res.matched_buckets.join(", ")}.`);
      res.warnings.forEach((w) => toast.warning(w));
      setFiles({ alt: [], pmay: [], other: [], purchase: [] });
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to match prices."),
  });

  const hasAnyUpload = Object.values(files).some((f) => f.length > 0);

  // -------------------------------------------------------------- rows --

  const saveRows = useMutation({
    mutationFn: (next: MappingRow[]) => api.updateMappingRows(houseId, next),
    onSuccess: (saved) => {
      setRows(saved);
      setRowsDirty(false);
      invalidate();
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save."),
  });

  // Local-only — nothing hits the server until the "บันทึก" button is clicked.
  const updateRow = (index: number, patch: Partial<MappingRow>) => {
    setRows((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));
    setRowsDirty(true);
  };

  const updatePurchasePrice = (index: number, field: "other_maker_price" | "unit_price", price: number) => {
    const orderType = field === "other_maker_price" ? "จัดซื้อ (บวกกำไร 10DK)" : "จัดซื้อ (ราคาจริง ไม่บวกกำไร)";
    updateRow(index, { [field]: price, ...(price > 0 && { order_type: orderType }) });
  };

  const handleSaveRows = () => saveRows.mutate(rows);

  const removeRow = (index: number) => {
    setRows((prev) => prev.filter((_, i) => i !== index));
    setRowsDirty(true);
  };

  const roomOptions = useMemo(() => {
    const seen = new Set<string>();
    const list: string[] = [];
    for (const r of rows) {
      if (!seen.has(r.room)) {
        seen.add(r.room);
        list.push(r.room);
      }
    }
    return list;
  }, [rows]);

  const handleRoomChange = (index: number, value: string) => {
    if (value === NEW_ROOM_OPTION) {
      const name = promptForNewRoom();
      if (name === null) return;
      updateRow(index, { room: name });
      return;
    }
    updateRow(index, { room: value });
  };

  // ----------------------------------------------------------- preview --

  const debouncedRows = useDebouncedValue(rows, 500);
  const preview = useQuery<PreviewResponse>({
    queryKey: ["preview", houseId, sheetName, debouncedRows, columnMapping],
    queryFn: () => api.preview(houseId, sheetName, debouncedRows, columnMapping),
    enabled: !!sheetName && debouncedRows.length > 0,
  });

  const grandTotal = preview.data ? previewGrandTotal(preview.data.rows) : 0;
  const hasAltPrice = preview.data?.rows.some((row) => row.g_cost != null) ?? false;
  // If the user hasn't typed a baseline (the furniture value from the very
  // first quotation) yet, fall back to the current 10DK Price grand total so
  // the 12% fee still computes instead of blocking on a warning. The typed
  // value (or its absence) is still what gets persisted/exported as the real
  // baseline — this fallback only fills in the live preview number.
  const hasCustomBaseline = baseline.trim() !== "" && !Number.isNaN(parseFloat(baseline));
  const baselineNum = hasCustomBaseline ? parseFloat(baseline) : grandTotal;
  const managementFeeBilled = baselineNum * 0.12;
  const netTotal = grandTotal + managementFeeBilled;

  // ------------------------------------------------------ loading factor --

  const loadingFactor =
    altInputs.sum > 0 ? (altInputs.sum + altInputs.protection + altInputs.management) / altInputs.sum : null;

  const applyLoadingFactor = useMutation({
    mutationFn: () =>
      api.applyLoadingFactor(houseId, sheetName, altInputs.sum, altInputs.protection, altInputs.management, columnMapping),
    onSuccess: (res) => {
      invalidate();
      refreshPriceCalculations();
      if (res.updated) {
        toast.success(`คำนวณใหม่และอัปเดต ${res.anchor_cell} = ${res.current_value} ให้อัตโนมัติแล้ว`);
      }
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to apply loading factor."),
  });

  // Applies as soon as the user edits any of the 3 fields (debounced, not
  // on blur) — altTouched gates this so merely loading the page with the
  // AI-extracted defaults doesn't immediately overwrite whatever value is
  // already in the template.
  const [altTouched, setAltTouched] = useState(false);
  const debouncedAltInputs = useDebouncedValue(altInputs, 500);
  useEffect(() => {
    if (altTouched && debouncedAltInputs.sum > 0) applyLoadingFactor.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [altTouched, debouncedAltInputs]);

  // ----------------------------------------------------- I / M multipliers --
  const vatAnchor = `${columnMapping.formula_i_col}${columnMapping.multiplier_anchor_row}`;
  const profitAnchor = `${columnMapping.formula_m_col}${columnMapping.multiplier_anchor_row}`;

  const [iFeePercent, setIFeePercent] = useState(5);
  const [iVatPercent, setIVatPercent] = useState(7);
  const iMultiplier = (1 + iFeePercent / 100) * (1 + iVatPercent / 100);

  const [mMultiplierInput, setMMultiplierInput] = useState("1.45");

  const setMultiplier = useMutation({
    mutationFn: (vars: { column: "i" | "m"; value: number }) =>
      api.setMultiplier(houseId, sheetName, vars.column, vars.value, columnMapping),
    onSuccess: (res) => {
      invalidate();
      refreshPriceCalculations();
      if (res.updated) {
        toast.success(`อัปเดต ${res.anchor_cell} = ${res.current_value} ให้อัตโนมัติแล้ว`);
      }
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to set multiplier."),
  });

  // Same touched-gated debounced auto-apply as Loading Factor above — I and
  // M each get their own "touched" flag since they're independent fields.
  const [iTouched, setITouched] = useState(false);
  const debouncedIMultiplier = useDebouncedValue(iMultiplier, 500);
  useEffect(() => {
    if (iTouched) setMultiplier.mutate({ column: "i", value: debouncedIMultiplier });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [iTouched, debouncedIMultiplier]);

  const [mTouched, setMTouched] = useState(false);
  const debouncedMMultiplierInput = useDebouncedValue(mMultiplierInput, 500);
  useEffect(() => {
    if (!mTouched) return;
    const v = parseFloat(debouncedMMultiplierInput);
    if (!Number.isNaN(v) && v > 0) setMultiplier.mutate({ column: "m", value: v });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mTouched, debouncedMMultiplierInput]);

  // ---------------------------------------------------------- baseline --

  const saveBaseline = useMutation({
    mutationFn: (value: number | null) => api.updateBaseline(houseId, value),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save baseline."),
  });

  // -------------------------------------------------------------- texts --

  const suspiciousItems = rows.filter((r) => r.suspicious);
  const mappingGroups = groupMappingRowsByRoom(rows);
  const notFoundItems = useMemo(
    () => rows.map((row, index) => ({ row, index })).filter(({ row }) =>
      isUnpriced(row) && row.item_name.replaceAll("[Price Not Found]", "").trim(),
    ),
    [rows],
  );
  const [notFoundOpen, setNotFoundOpen] = useState(true);
  const hasQuotations = (house.data?.quotation_buckets.length ?? 0) > 0;

  const rawTexts = useQuery({
    queryKey: ["quotation-texts", houseId],
    queryFn: () => api.getQuotationTexts(houseId),
    enabled: hasQuotations,
  });
  const pdfFiles = useQuery({
    queryKey: ["quotation-pdfs", houseId],
    queryFn: () => api.getQuotationPdfs(houseId),
    enabled: hasQuotations,
  });

  // --------------------------------------------------------------- export --

  const exportUnpricedItems = useMutation({
    mutationFn: () => api.downloadUnpricedItems(houseId, notFoundItems.map(({ row }) => row)),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "price-not-found.xlsx";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "ดาวน์โหลดรายการที่ไม่พบราคาไม่สำเร็จ"),
  });

  const exportExcel = useMutation({
    mutationFn: () =>
      api.exportExcel(houseId, sheetName, rows, columnMapping, grandTotal, baseline ? baselineNum : null),
    onSuccess: (res) => {
      invalidate();
      exportStatus.refetch();
      exportVersions.refetch();
      toast.success("สร้างไฟล์ Excel เรียบร้อยแล้ว — บันทึกเป็นเวอร์ชันใหม่ ดาวน์โหลดได้ด้านล่าง");
      res.warnings.forEach((w) => toast.warning(w));
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to export."),
  });

  const exportStatus = useQuery({
    queryKey: ["export-status", houseId, rows],
    queryFn: () => api.exportStatus(houseId, rows),
    enabled: !!house.data?.has_final_export,
  });

  const exportVersions = useQuery<ExportVersionMeta[]>({
    queryKey: ["export-versions", houseId],
    queryFn: () => api.listExportVersions(houseId),
    enabled: !!house.data?.has_final_export,
  });

  const sheetNames = house.data?.sheet_names ?? initial.sheet_names;
  const hasFinalExport = house.data?.has_final_export ?? initial.has_final_export;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Step 3 — Upload Supplier Quotation & Final Mapping" />
        <div className="space-y-5 p-5">
          <Field label="Target sheet in the Excel template">
            <Select value={sheetName} onChange={(e) => setSheetName(e.target.value)}>
              {sheetNames.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </Select>
          </Field>

          <p className="text-sm text-slate-500">
            แยกอัปโหลดตามประเภทซัพพลายเออร์ 4 ช่อง — เพื่อให้ระบบกำหนด <strong>ประเภท/สูตรราคา</strong> ถูกต้อง 100%
            ตามที่คุณเลือกเอง
          </p>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {BUCKETS.map((b) => (
              <div key={b.key}>
                <p className="mb-1 flex items-center gap-1.5 text-sm font-medium text-slate-700">
                  <b.icon size={14} className="text-slate-400" /> {b.title}
                </p>
                {b.hint && <p className="mb-1 text-xs text-slate-400">{b.hint}</p>}
                <input
                  type="file"
                  accept=".pdf"
                  multiple
                  onChange={(e) => setFiles((prev) => ({ ...prev, [b.key]: Array.from(e.target.files ?? []) }))}
                  className="w-full text-xs text-slate-600 file:mr-2 file:rounded-lg file:border-0 file:bg-slate-100 file:px-2 file:py-1.5 file:text-xs file:font-medium file:text-slate-700 hover:file:bg-slate-200"
                />
                {files[b.key].length > 0 && (
                  <p className="mt-1 text-xs text-emerald-600">{files[b.key].length} file(s) selected</p>
                )}
              </div>
            ))}
          </div>

          <Button
            onClick={() => matchPrices.mutate()}
            disabled={!hasAnyUpload || !sheetName || matchPrices.isPending || rowsDirty}
          >
            {matchPrices.isPending ? <Spinner /> : <Link2 size={16} />}
            Match Prices &amp; Generate Excel
          </Button>
          {rowsDirty && (
            <p className="text-xs text-amber-600">
              บันทึกการแก้ไขในตารางด้านล่างก่อน ถึงจะจับคู่ราคาใหม่ได้ — ไม่งั้นการแก้ไขที่ยังไม่บันทึกจะหายไป
            </p>
          )}
        </div>
      </Card>

      {rows.length > 0 && (
        <>
          {(altInputs.sum > 0 || hasAltPrice) && (
            <Card>
              <CardHeader icon={<Ruler size={16} />} title="Loading Factor (ALT) — แก้ไขค่าได้ที่นี่" />
              <div className="space-y-4 p-5">
                <p className="text-xs text-slate-500">
                  ระบบดึงยอดจากใบเสนอราคา ALT ให้เมื่ออ่านข้อมูลได้ครบ คุณกรอกหรือแก้ยอดได้ที่นี่
                </p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                  <Field label="ผลรวมต้นทุนรายการ (บาท)">
                    <Input
                      type="number"
                      value={altInputs.sum}
                      onChange={(e) => {
                        setAltInputs((p) => ({ ...p, sum: Number(e.target.value) || 0 }));
                        setAltTouched(true);
                      }}
                    />
                  </Field>
                  <Field label="ค่า Protection พื้น (บาท)">
                    <Input
                      type="number"
                      value={altInputs.protection}
                      onChange={(e) => {
                        setAltInputs((p) => ({ ...p, protection: Number(e.target.value) || 0 }));
                        setAltTouched(true);
                      }}
                    />
                  </Field>
                  <Field label="ค่าดำเนินการ (บาท)">
                    <Input
                      type="number"
                      value={altInputs.management}
                      onChange={(e) => {
                        setAltInputs((p) => ({ ...p, management: Number(e.target.value) || 0 }));
                        setAltTouched(true);
                      }}
                    />
                  </Field>
                </div>
                {loadingFactor && (
                  <div className="flex gap-8">
                    <div>
                      <p className="text-xs text-slate-400">Loading Factor</p>
                      <p className="text-lg font-semibold tabular-nums">{loadingFactor.toFixed(4)}</p>
                    </div>
                    <div>
                      <p className="text-xs text-slate-400">เพิ่มขึ้นกี่ %</p>
                      <p className="text-lg font-semibold tabular-nums">+{((loadingFactor - 1) * 100).toFixed(1)}%</p>
                    </div>
                  </div>
                )}
              </div>
            </Card>
          )}

          <Card>
            <CardHeader
              icon={<Settings size={16} />}
              title="ตัวคูณในไฟล์ Excel"
              description="ถ้าช่องตัวคูณในเทมเพลตว่าง ระบบใช้ 1.1235 และ 1.45 ให้โดยอัตโนมัติ"
            />
            <div className="space-y-5 p-5">
              <div>
                <p className="mb-2 text-sm font-medium text-slate-700">ตัวคูณ +5%+VAT7% (ช่อง {vatAnchor})</p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
                  <Field label="ค่าธรรมเนียม/กำไร (%)">
                    <Input
                      type="number"
                      value={iFeePercent}
                      onChange={(e) => {
                        setIFeePercent(Number(e.target.value) || 0);
                        setITouched(true);
                      }}
                    />
                  </Field>
                  <Field label="VAT (%)">
                    <Input
                      type="number"
                      value={iVatPercent}
                      onChange={(e) => {
                        setIVatPercent(Number(e.target.value) || 0);
                        setITouched(true);
                      }}
                    />
                  </Field>
                  <div>
                    <p className="text-xs text-slate-400">ตัวคูณที่จะเขียนลงช่อง {vatAnchor}</p>
                    <p className="text-lg font-semibold tabular-nums">{iMultiplier.toFixed(4)}</p>
                  </div>
                </div>
              </div>

              <div>
                <p className="mb-2 text-sm font-medium text-slate-700">ตัวคูณกำไร 10DK (ช่อง {profitAnchor})</p>
                <Field label="ใส่ตัวคูณตรงๆ — ค่าเริ่มต้น 1.45 (กำไร 45%)" hint="แก้ไขได้ถ้างานนี้ใช้ % ต่างจากปกติ">
                  <Input
                    type="number"
                    step="0.01"
                    value={mMultiplierInput}
                    onChange={(e) => {
                      setMMultiplierInput(e.target.value);
                      setMTouched(true);
                    }}
                  />
                </Field>
              </div>
            </div>
          </Card>

          <Card>
            <CardHeader icon={<Coins size={16} />} title="คำนวณค่าดำเนินการ 10DK 12% (อ้างอิงเท่านั้น ไม่เขียนลงไฟล์)" />
            <div className="space-y-3 p-5">
              <Field
                label="มูลค่าเฟอร์นิเจอร์ในใบเสนอราคาแรกสุด (บาท) — กรอกครั้งเดียว ใช้เป็นฐานคิดค่าธรรมเนียมจริง"
                hint="ถ้าไม่กรอก ระบบจะใช้ยอดรวม 10DK Price ปัจจุบันแทนโดยอัตโนมัติ"
              >
                <Input
                  type="number"
                  value={baseline}
                  onChange={(e) => setBaseline(e.target.value)}
                  onBlur={() => saveBaseline.mutate(baseline ? parseFloat(baseline) : null)}
                />
              </Field>
              {baselineNum > 0 && (
                <p className="text-sm text-slate-600">
                  ค่าดำเนินการ 12% จาก{hasCustomBaseline ? "มูลค่าฐาน" : "ยอดรวมปัจจุบัน (ยังไม่ได้กรอกมูลค่าฐาน)"}:{" "}
                  <strong className="tabular-nums">
                    {managementFeeBilled.toLocaleString("en-US", { maximumFractionDigits: 0 })}
                  </strong>{" "}
                  บาท
                </p>
              )}
            </div>
          </Card>

          {hasQuotations && (
            <Card>
              <CardHeader icon={<Search size={16} />} title="ดูข้อความดิบที่ดึงจาก PDF ใบเสนอราคา" />
              <div className="space-y-4 p-5 text-sm">
                {rawTexts.data &&
                  Object.entries(rawTexts.data.texts).map(([bucket, text]) => (
                    <details key={bucket}>
                      <summary className="cursor-pointer font-medium text-slate-700">{bucket}</summary>
                      <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
                        {text}
                      </pre>
                    </details>
                  ))}
                {pdfFiles.data && pdfFiles.data.length > 0 && (
                  <div>
                    <p className="mb-2 flex items-center gap-1.5 font-medium text-slate-700">
                      <Paperclip size={14} /> ไฟล์ PDF ต้นฉบับ
                    </p>
                    <ul className="space-y-1">
                      {pdfFiles.data.map((f) => (
                        <li key={f.id}>
                          <a
                            href={api.quotationPdfUrl(houseId, f.id)}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-1.5 text-indigo-600 hover:underline"
                          >
                            <FileText size={14} /> {f.bucket_label} — {f.filename}
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </Card>
          )}

          <Card>
            <CardHeader
              icon={<Pencil size={16} />}
              title="ตรวจสอบและแก้ไขก่อนบันทึก"
              description="เลือกประเภทให้ถูกต้องต่อรายการ ตาม 3 วิธีคิดราคา — ดูคำอธิบายในแต่ละช่อง — แก้ไขเสร็จแล้วอย่าลืมกด &quot;บันทึก&quot;"
              right={
                <div className="flex items-center gap-2">
                  {rowsDirty && <span className="text-xs font-medium text-amber-600">มีการแก้ไขที่ยังไม่บันทึก</span>}
                  <Button onClick={handleSaveRows} disabled={!rowsDirty || saveRows.isPending}>
                    {saveRows.isPending ? <Spinner /> : <Save size={16} />}
                    บันทึก
                  </Button>
                </div>
              }
            />
            <div className="p-5">
              {suspiciousItems.length > 0 && (
                <div className="mb-4">
                  <Alert tone="warning">
                    พบ {suspiciousItems.length} รายการที่ราคาต่ำผิดปกติ (ต่ำกว่า 100 บาท) — มักเกิดจาก AI อ่านตัวเลขที่มี
                    comma คั่นหลักพันผิด กรุณาเปิด &quot;ดูข้อความดิบ&quot; ด้านบนเพื่อตรวจราคาที่ถูกต้อง
                  </Alert>
                </div>
              )}
              <div className="overflow-x-auto">
                <table className="w-full min-w-[1450px] border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                      <th className="py-2 pr-2">Room</th>
                      <th className="py-2 pr-2">Item Name</th>
                      <th className="py-2 pr-2">ขนาด</th>
                      <th className="py-2 pr-2">Spec</th>
                      <th className="w-20 py-2 pr-2">Qty</th>
                      <th className="w-48 py-2 pr-2">ประเภท</th>
                      <th className="w-24 py-2 pr-2">ALT</th>
                      <th className="w-24 py-2 pr-2">P&apos;May</th>
                      <th className="w-24 py-2 pr-2">Other (บวกกำไร)</th>
                      <th className="w-24 py-2 pr-2">เบิกจ่ายตามจริง</th>
                      <th className="w-28 py-2 pr-2">Supplier</th>
                      <th className="w-10 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {mappingGroups.map((group) => (
                      <Fragment key={group.room || "__empty__"}>
                        <tr className="bg-slate-900 text-white">
                          <td className="px-2 py-1.5" colSpan={12}>
                            {group.room || UNSPECIFIED_LABEL}{" "}
                            <span className="font-normal text-slate-300">({group.entries.length} รายการ)</span>
                          </td>
                        </tr>
                        {group.entries.map(({ row, index: i }) => {
                          const isNotFound = isUnpriced(row);
                          return (
                          <tr
                            key={i}
                            className={
                              "border-b border-slate-100 " + (row.suspicious ? "bg-amber-50" : isNotFound ? "bg-red-50" : "")
                            }
                          >
                            <td className="py-1.5 pr-2">
                              <Select value={row.room} onChange={(e) => handleRoomChange(i, e.target.value)}>
                                {roomOptions.map((r) => (
                                  <option key={r || "__empty__"} value={r}>
                                    {r || UNSPECIFIED_LABEL}
                                  </option>
                                ))}
                                <option value={NEW_ROOM_OPTION}>+ ห้องใหม่...</option>
                              </Select>
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                value={row.item_name}
                                onChange={(e) => updateRow(i, { item_name: e.target.value })}
                                className={isNotFound ? "text-red-600 font-medium" : undefined}
                              />
                              {row.suspicious && (
                                <Badge tone="warning">
                                  <span className="flex items-center gap-1">
                                    <AlertTriangle size={12} /> เช็คราคา
                                  </span>
                                </Badge>
                              )}
                              {isNotFound && (
                                <Badge tone="danger">
                                  <span className="flex items-center gap-1">
                                    <XCircle size={12} /> ไม่พบราคา
                                  </span>
                                </Badge>
                              )}
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                value={row.spec}
                                placeholder="เช่น 180x200cm, ไม้วีเนียร์"
                                onChange={(e) => updateRow(i, { spec: e.target.value })}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                value={row.quotation_spec}
                                placeholder="รายละเอียดจากใบเสนอราคา (AI ดึงให้อัตโนมัติ, แก้ไขได้)"
                                onChange={(e) => updateRow(i, { quotation_spec: e.target.value })}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                type="number"
                                value={row.quantity}
                                onChange={(e) => updateRow(i, { quantity: Number(e.target.value) || 0 })}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Select
                                value={row.order_type}
                                onChange={(e) => updateRow(i, { order_type: e.target.value as MappingRow["order_type"] })}
                              >
                                {ORDER_TYPES.map((t) => (
                                  <option key={t} value={t}>
                                    {t}
                                  </option>
                                ))}
                              </Select>
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                type="number"
                                value={row.alt_price}
                                onChange={(e) => updateRow(i, { alt_price: Number(e.target.value) || 0 })}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                type="number"
                                value={row.pmay_price}
                                onChange={(e) => updateRow(i, { pmay_price: Number(e.target.value) || 0 })}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                type="number"
                                value={row.other_maker_price}
                                onChange={(e) => updatePurchasePrice(i, "other_maker_price", Number(e.target.value) || 0)}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input
                                type="number"
                                value={row.unit_price}
                                onChange={(e) => updatePurchasePrice(i, "unit_price", Number(e.target.value) || 0)}
                              />
                            </td>
                            <td className="py-1.5 pr-2">
                              <Input value={row.supplier} onChange={(e) => updateRow(i, { supplier: e.target.value })} />
                            </td>
                            <td className="py-1.5 text-center">
                              <button onClick={() => removeRow(i)} aria-label="Remove row" className="text-slate-400 hover:text-red-600">
                                <X size={16} />
                              </button>
                            </td>
                          </tr>
                          );
                        })}
                      </Fragment>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-3">
                <Button
                  variant="secondary"
                  onClick={() => {
                    setRows((prev) => [...prev, emptyRow()]);
                    setRowsDirty(true);
                  }}
                >
                  + Add row
                </Button>
              </div>
            </div>
          </Card>

          {notFoundItems.length > 0 && (
            <Card>
              <div className="flex flex-wrap items-center gap-3 border-b border-slate-100 px-5 py-4">
                <button
                  type="button"
                  onClick={() => setNotFoundOpen((v) => !v)}
                  aria-expanded={notFoundOpen}
                  className="flex flex-1 basis-64 items-center justify-between gap-3 text-left"
                >
                  <h2 className="flex items-center gap-2 text-base font-semibold text-slate-900">
                    <Search size={16} className="shrink-0 text-slate-400" />
                    {`ค้นหาราคาในใบเสนอราคาดิบ สำหรับ ${notFoundItems.length} รายการที่ยังไม่พบราคา`}
                  </h2>
                  <ChevronDown
                    size={16}
                    className={cn("shrink-0 text-slate-400 transition-transform", notFoundOpen && "rotate-180")}
                  />
                </button>
                <Button variant="secondary" onClick={() => exportUnpricedItems.mutate()} disabled={exportUnpricedItems.isPending}>
                  {exportUnpricedItems.isPending ? <Spinner /> : <Download size={16} />}
                  ดาวน์โหลด Excel รายการที่ไม่พบราคา
                </Button>
              </div>
              {notFoundOpen && (
                <div className="space-y-4 p-5">
                  {notFoundItems.map(({ row, index }) => (
                    <NotFoundSearch key={`${index}:${row.item_name}`} houseId={houseId} itemName={row.item_name} />
                  ))}
                </div>
              )}
            </Card>
          )}

          {preview.data && preview.data.rows.length > 0 && (
            <Card>
              <CardHeader icon={<Eye size={16} />} title="Preview ตารางเต็ม (จำลองหน้าตา Excel จริง พร้อมสูตรที่จะผูกให้)" />
              <div className="space-y-4 p-5">
                {preview.data.warnings.length > 0 && (
                  <div className="space-y-2">
                    {preview.data.warnings.map((w, i) => (
                      <Alert key={i} tone="warning">
                        {w}
                      </Alert>
                    ))}
                  </div>
                )}
                <ExcelStylePreview rows={preview.data.rows} />
                <p className="text-sm text-slate-600">
                  ยอดรวมค่าเฟอร์นิเจอร์ (10DK Price + งานจัดซื้อเบิกจ่ายตามราคาจริง):{" "}
                  <strong className="tabular-nums">{grandTotal.toLocaleString("en-US", { maximumFractionDigits: 0 })} บาท</strong>
                </p>

                {grandTotal > 0 && (
                  <>
                    <div className="grid grid-cols-3 gap-4 border-t border-slate-100 pt-4">
                      <Stat label="ยอดค่าเฟอร์นิเจอร์" value={`${grandTotal.toLocaleString("en-US", { maximumFractionDigits: 0 })} บาท`} />
                      <Stat label="ค่าดำเนินการ 12%" value={`${managementFeeBilled.toLocaleString("en-US", { maximumFractionDigits: 0 })} บาท`} />
                      <Stat label="ราคารวมสุทธิ" value={`${netTotal.toLocaleString("en-US", { maximumFractionDigits: 0 })} บาท`} highlight />
                    </div>
                    {!hasCustomBaseline && (
                      <Alert tone="info">
                        ยังไม่ได้กรอก &apos;มูลค่าเฟอร์นิเจอร์ในใบเสนอราคาแรกสุด&apos; ด้านบน — ตัวเลขค่าดำเนินการ 12%
                        ด้านบนนี้คำนวณจากยอดรวมปัจจุบันไปก่อนชั่วคราว (แทนที่จะเป็นยอดจากใบเสนอราคาแรกสุดจริง) กรอกด้านบนถ้าต้องการความแม่นยำ
                      </Alert>
                    )}
                  </>
                )}

                <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-4">
                  <Button onClick={() => exportExcel.mutate()} disabled={!sheetName || exportExcel.isPending}>
                    {exportExcel.isPending ? <Spinner /> : <CheckCircle2 size={16} />}
                    ยืนยันและสร้างไฟล์ Excel
                  </Button>
                  {hasFinalExport && (
                    <>
                      {exportStatus.data?.is_stale && (
                        <Alert tone="warning">
                          มีการแก้ไขข้อมูลหลังจากสร้างไฟล์นี้ล่าสุด — กด &quot;ยืนยันและสร้างไฟล์ Excel&quot; ใหม่ก่อนดาวน์โหลด
                        </Alert>
                      )}
                      <a
                        href={api.exportFileUrl(houseId)}
                        className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                      >
                        <Download size={16} /> Download Completed Excel File
                      </a>
                    </>
                  )}
                </div>

                {hasFinalExport && exportVersions.data && exportVersions.data.length > 0 && (
                  <div className="border-t border-slate-100 pt-4">
                    <p className="mb-2 flex items-center gap-1.5 text-sm font-medium text-slate-700">
                      <Clock size={14} /> ประวัติเวอร์ชันที่ export ไว้ ({exportVersions.data.length})
                    </p>
                    <ul className="space-y-1.5">
                      {exportVersions.data.map((v, i) => (
                        <li key={v.id} className="flex items-center justify-between gap-3 text-sm">
                          <span className="text-slate-600">
                            {formatDateTime(v.created_at)}
                            {i === 0 && (
                              <span className="ml-2">
                                <Badge tone="success">ล่าสุด</Badge>
                              </span>
                            )}
                          </span>
                          <a
                            href={api.exportVersionFileUrl(houseId, v.id)}
                            className="flex items-center gap-1.5 text-indigo-600 hover:underline"
                          >
                            <Download size={14} /> ดาวน์โหลด
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div>
      <p className="text-xs text-slate-400">{label}</p>
      <p className={"tabular-nums text-lg font-semibold " + (highlight ? "text-indigo-600" : "text-slate-900")}>{value}</p>
    </div>
  );
}

function NotFoundSearch({ houseId, itemName }: { houseId: string; itemName: string }) {
  const cleanName = itemName.split(" [")[0].trim();
  const [query, setQuery] = useState(cleanName);
  const debouncedQuery = useDebouncedValue(query, 400);
  const results = useQuery({
    queryKey: ["quotation-search", houseId, debouncedQuery],
    queryFn: () => api.searchQuotationText(houseId, debouncedQuery),
    enabled: debouncedQuery.trim().length >= 2,
  });

  return (
    <div className="border-b border-slate-100 pb-4 last:border-0">
      <Field label={`คำค้นหาสำหรับ: ${cleanName}`}>
        <Input value={query} onChange={(e) => setQuery(e.target.value)} />
      </Field>
      <div className="mt-2 space-y-2">
        {results.data && results.data.length > 0 ? (
          results.data.map((r) => (
            <div key={r.bucket_label}>
              <p className="flex items-center gap-1 text-xs font-medium text-slate-600">
                <FileText size={12} /> พบใน {r.bucket_label}:
              </p>
              <pre className="mt-1 whitespace-pre-wrap rounded-lg bg-slate-50 p-2 text-xs text-slate-600">
                {r.matches.join("\n")}
              </pre>
            </div>
          ))
        ) : (
          <p className="text-xs text-slate-400">ไม่พบข้อความที่ตรงกับคำค้นหานี้ในใบเสนอราคาใดเลย</p>
        )}
      </div>
    </div>
  );
}
