"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileEdit, FileSignature, FileText, Image as ImageIcon, Plus, Save, Trash2 } from "lucide-react";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { DEFAULT_COLUMN_MAPPING } from "@/lib/types";
import type {
  AttachmentType,
  ContractAttachmentMeta,
  ContractAttachmentUpload,
  ContractDetails,
  QuotationPreview as QuotationPreviewData,
  QuotationRow,
} from "@/lib/types";
import { houseKey, useHouse } from "@/hooks/useHouse";
import { Alert, Button, Card, CardHeader, Field, Input, Select, Spinner, Textarea } from "@/components/ui/primitives";
import { QuotationPreviewTable, quotationTotals } from "@/components/QuotationPreview";
import { ContractAttachmentCanvas, type AttachmentCanvasHandle } from "@/components/ContractAttachmentCanvas";
import { useToast } from "@/components/ui/Toast";

type TabKey = "form" | "attachments" | "price";

const TABS: { key: TabKey; label: string; icon: typeof FileSignature }[] = [
  { key: "form", label: "สัญญาฟอร์ม", icon: FileSignature },
  { key: "attachments", label: "เอกสารแนบ", icon: ImageIcon },
  { key: "price", label: "ใบราคา", icon: FileText },
];

const ATTACHMENT_TYPES: { value: AttachmentType; label: string }[] = [
  { value: "", label: "(ไม่ระบุ)" },
  { value: "plan", label: "แปลน (Plan)" },
  { value: "perspective", label: "ภาพเพอร์สเปคทีฟ (Perspective)" },
  { value: "furniture_list", label: "รายการเฟอร์นิเจอร์ (Furniture List)" },
];

interface DraftPage {
  key: string;
  title: string;
  imageUrl: string | null;
  attachmentType: AttachmentType;
  floor: string;
  zone: string;
  itemRange: string;
  referenceNote: string;
}

export default function ContractPage() {
  const { houseId } = useParams<{ projectId: string; houseId: string }>();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<TabKey>("form");
  const house = useHouse(houseId);

  // ------------------------------------------------------------ สัญญาฟอร์ม --

  const contractQuery = useQuery({
    queryKey: ["contract-details", houseId],
    queryFn: () => api.getContractDetails(houseId),
  });

  const [details, setDetails] = useState<ContractDetails | null>(null);
  const [detailsSyncedFrom, setDetailsSyncedFrom] = useState<ContractDetails | undefined>(undefined);
  if (contractQuery.data && contractQuery.data !== detailsSyncedFrom) {
    setDetailsSyncedFrom(contractQuery.data);
    setDetails(contractQuery.data);
  }

  const setField = <K extends keyof ContractDetails>(key: K, value: ContractDetails[K]) =>
    setDetails((prev) => (prev ? { ...prev, [key]: value } : prev));

  const parseNum = (raw: string): number | null => (raw.trim() === "" ? null : Number(raw) || 0);

  const saveDetails = useMutation({
    mutationFn: (d: ContractDetails) => api.updateContractDetails(houseId, d),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: houseKey(houseId) });
      toast.success("บันทึกสัญญาแล้ว");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save contract."),
  });

  // ------------------------------------------------------------- เอกสารแนบ --

  const attachmentsQuery = useQuery({
    queryKey: ["contract-attachments", houseId],
    queryFn: () => api.listContractAttachments(houseId),
  });

  const [pages, setPages] = useState<DraftPage[] | null>(null);
  const [pagesSyncedFrom, setPagesSyncedFrom] = useState<ContractAttachmentMeta[] | undefined>(undefined);
  if (attachmentsQuery.data && attachmentsQuery.data !== pagesSyncedFrom) {
    setPagesSyncedFrom(attachmentsQuery.data);
    setPages(
      attachmentsQuery.data.map((m) => ({
        key: `existing-${m.id}`,
        title: m.title,
        imageUrl: api.contractAttachmentUrl(houseId, m.id),
        attachmentType: m.attachment_type,
        floor: m.floor,
        zone: m.zone,
        itemRange: m.item_range,
        referenceNote: m.reference_note,
      }))
    );
  }

  const canvasRefs = useRef<Record<string, AttachmentCanvasHandle | null>>({});

  // "ชื่อ Zone" suggestions — pulled from room names already recorded during
  // the calc step (furniture_list + mapping_rows), so the user can reuse an
  // existing room name instead of retyping it. Kept as free-text with a
  // <datalist> (not a strict Select) since a zone label is often the room
  // name plus extra context (e.g. "Living & Dining Area" vs. just "Living").
  const roomOptions = Array.from(
    new Set(
      [...(house.data?.furniture_list.map((f) => f.room) ?? []), ...(house.data?.mapping_rows.map((r) => r.room) ?? [])]
        .map((r) => r.trim())
        .filter(Boolean)
    )
  ).sort((a, b) => a.localeCompare(b, "th"));

  const addPage = () => {
    setPages((prev) => {
      const list = prev ?? [];
      return [
        ...list,
        {
          key: `new-${Date.now()}`,
          title: `เอกสารแนบ (${list.length + 1})`,
          imageUrl: null,
          attachmentType: "",
          floor: "",
          zone: "",
          itemRange: "",
          referenceNote: "",
        },
      ];
    });
  };

  const removePage = (key: string) => {
    delete canvasRefs.current[key];
    setPages((prev) => (prev ?? []).filter((p) => p.key !== key));
  };

  const updatePage = (key: string, patch: Partial<DraftPage>) => {
    setPages((prev) => (prev ?? []).map((p) => (p.key === key ? { ...p, ...patch } : p)));
  };

  const saveAttachments = useMutation({
    mutationFn: async () => {
      const list = pages ?? [];
      const items: { meta: ContractAttachmentUpload; blob: Blob }[] = [];
      for (const p of list) {
        const handle = canvasRefs.current[p.key];
        const blob = await handle?.toBlob();
        if (blob) {
          items.push({
            meta: {
              title: p.title,
              attachment_type: p.attachmentType,
              floor: p.floor,
              zone: p.zone,
              item_range: p.itemRange,
              reference_note: p.referenceNote,
            },
            blob,
          });
        }
      }
      return api.uploadContractAttachments(houseId, items);
    },
    onSuccess: (meta) => {
      queryClient.invalidateQueries({ queryKey: houseKey(houseId) });
      queryClient.invalidateQueries({ queryKey: ["contract-attachments", houseId] });
      toast.success(`บันทึกเอกสารแนบแล้ว (${meta.length} หน้า)`);
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to save attachments."),
  });

  // ------------------------------------------------------------------ ใบราคา --

  const pricePreview = useQuery<QuotationPreviewData>({
    queryKey: ["contract-price-preview", houseId, house.data?.target_sheet_name],
    queryFn: () =>
      api.previewQuotationFromHouse(
        houseId,
        house.data!.target_sheet_name!,
        house.data!.mapping_rows,
        DEFAULT_COLUMN_MAPPING
      ),
    enabled: !!house.data?.target_sheet_name && (house.data?.mapping_rows.length ?? 0) > 0,
  });

  const [priceRows, setPriceRows] = useState<QuotationRow[] | null>(null);
  const [priceSyncedFrom, setPriceSyncedFrom] = useState<QuotationPreviewData | undefined>(undefined);
  if (pricePreview.data && pricePreview.data !== priceSyncedFrom) {
    setPriceSyncedFrom(pricePreview.data);
    setPriceRows(pricePreview.data.rows);
  }

  const updatePriceRow = (index: number, patch: Partial<QuotationRow>) =>
    setPriceRows((prev) => (prev ? prev.map((r, i) => (i === index ? { ...r, ...patch } : r)) : prev));
  const updatePriceRange = (start: number, end: number, patch: Partial<QuotationRow>) =>
    setPriceRows((prev) => (prev ? prev.map((r, i) => (i >= start && i < end ? { ...r, ...patch } : r)) : prev));

  const [depositDeduction, setDepositDeduction] = useState(0);
  const [remarks, setRemarks] = useState("");
  const [grandTotalNote, setGrandTotalNote] = useState("(ไม่รวมรายการ TBC ค่าขนส่ง, ค่าประกอบและค่าติดตั้ง)");
  const totals = quotationTotals(priceRows ?? [], depositDeduction);

  // -------------------------------------------------------- ดาวน์โหลดสัญญา --

  const triggerDownload = (blob: Blob, filename: string) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  const downloadPdf = useMutation({
    mutationFn: () =>
      api.downloadContractPdf(houseId, {
        rows: priceRows ?? [],
        deposit_deduction: depositDeduction,
        remarks,
        grand_total_note: grandTotalNote,
      }),
    onSuccess: (blob) => triggerDownload(blob, "สัญญาจ้างตกแต่งภายใน.pdf"),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to generate the contract PDF."),
  });

  const downloadDocx = useMutation({
    mutationFn: () =>
      api.downloadContractDocx(houseId, {
        rows: priceRows ?? [],
        deposit_deduction: depositDeduction,
        remarks,
        grand_total_note: grandTotalNote,
      }),
    onSuccess: (blob) => triggerDownload(blob, "สัญญาจ้างตกแต่งภายใน.docx"),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to generate the contract Word file."),
  });

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-slate-900">
            <FileSignature size={24} /> ทำสัญญา
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            กรอกสัญญา แนบเอกสารประกอบ แล้วรวมกับใบราคาเป็น PDF ฉบับเดียวสำหรับส่งลูกค้า
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => downloadDocx.mutate()} disabled={downloadDocx.isPending}>
            {downloadDocx.isPending ? <Spinner /> : <FileEdit size={16} />}
            ดาวน์โหลดสัญญา (Word)
          </Button>
          <Button onClick={() => downloadPdf.mutate()} disabled={downloadPdf.isPending}>
            {downloadPdf.isPending ? <Spinner /> : <FileText size={16} />}
            ดาวน์โหลดสัญญา (PDF)
          </Button>
        </div>
      </header>

      <nav className="mb-6 flex gap-1 border-b border-slate-200">
        {TABS.map((t) => {
          const Icon = t.icon;
          return (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={
                "flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors " +
                (tab === t.key
                  ? "border-[var(--accent)] text-[var(--accent)]"
                  : "border-transparent text-slate-500 hover:text-slate-800")
              }
            >
              <Icon size={16} />
              {t.label}
            </button>
          );
        })}
      </nav>

      {tab === "form" && (
        <>
          {contractQuery.isLoading && (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> กำลังโหลด...
            </div>
          )}
          {details && (
            <div className="space-y-6">
              <Card>
                <CardHeader title="หัวสัญญา" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
                  <Field label="บ้านเลขที่ / โครงการ" hint='เช่น "บ้านเลขที่ 9/42 โครงการ The Gentry เกษตร-นวมินทร์"'>
                    <Input
                      value={details.property_description}
                      onChange={(e) => setField("property_description", e.target.value)}
                    />
                  </Field>
                  <Field label="วันที่ทำสัญญา">
                    <Input value={details.contract_date} onChange={(e) => setField("contract_date", e.target.value)} />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader title="ผู้ว่าจ้าง" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
                  <Field label="ชื่อ-นามสกุล">
                    <Input value={details.client_name} onChange={(e) => setField("client_name", e.target.value)} />
                  </Field>
                  <Field label="เลขที่บัตรประชาชน">
                    <Input
                      value={details.client_id_number}
                      onChange={(e) => setField("client_id_number", e.target.value)}
                    />
                  </Field>
                  <div className="sm:col-span-2">
                    <Field label="ที่อยู่">
                      <Textarea
                        rows={2}
                        value={details.client_address}
                        onChange={(e) => setField("client_address", e.target.value)}
                      />
                    </Field>
                  </div>
                </div>
              </Card>

              <Card>
                <CardHeader title="ผู้รับจ้าง" description="ค่าเริ่มต้นมาจากเอกสารสัญญามาตรฐาน แก้ไขได้ถ้าจำเป็น" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
                  <Field label="ชื่อบริษัท">
                    <Input value={details.contractor_name} onChange={(e) => setField("contractor_name", e.target.value)} />
                  </Field>
                  <Field label="ผู้ลงนาม (กรรมการผู้มีอำนาจ)">
                    <Input
                      value={details.contractor_signatory}
                      onChange={(e) => setField("contractor_signatory", e.target.value)}
                    />
                  </Field>
                  <Field label="ตำแหน่ง">
                    <Input value={details.contractor_title} onChange={(e) => setField("contractor_title", e.target.value)} />
                  </Field>
                  <Field label="ที่อยู่บริษัท">
                    <Input
                      value={details.contractor_address}
                      onChange={(e) => setField("contractor_address", e.target.value)}
                    />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader
                  title="ข้อ 1 — รายการเฟอร์นิเจอร์อ้างอิง"
                  description="อ้างอิงลำดับ/หน้าในเอกสารแนบ (เอกสารแนบ (10) และ (17) ในต้นแบบ)"
                />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-4">
                  <Field label="รายการที่ (สั่งผลิต)">
                    <Input
                      value={details.included_item_range}
                      onChange={(e) => setField("included_item_range", e.target.value)}
                      placeholder="เช่น 1"
                    />
                  </Field>
                  <Field label="หน้าเอกสารแนบ">
                    <Input
                      value={details.included_item_page}
                      onChange={(e) => setField("included_item_page", e.target.value)}
                      placeholder="เช่น (10)"
                    />
                  </Field>
                  <Field label="รายการที่ (ยกเว้น/จัดซื้อเอง)">
                    <Input
                      value={details.excluded_item_range}
                      onChange={(e) => setField("excluded_item_range", e.target.value)}
                      placeholder="เช่น A-L"
                    />
                  </Field>
                  <Field label="หน้าเอกสารแนบ">
                    <Input
                      value={details.excluded_item_page}
                      onChange={(e) => setField("excluded_item_page", e.target.value)}
                      placeholder="เช่น (17)"
                    />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader title="ข้อ 2 — ค่าจ้างและงวดชำระ" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-3">
                  <Field label="รวมเป็นเงิน (บาท)">
                    <Input
                      type="number"
                      value={details.total_price ?? ""}
                      onChange={(e) => setField("total_price", parseNum(e.target.value))}
                    />
                  </Field>
                  <Field label="งวดที่ 1 (บาท)">
                    <Input
                      type="number"
                      value={details.installment_1_amount ?? ""}
                      onChange={(e) => setField("installment_1_amount", parseNum(e.target.value))}
                    />
                  </Field>
                  <Field label="งวดที่ 2 (บาท)">
                    <Input
                      type="number"
                      value={details.installment_2_amount ?? ""}
                      onChange={(e) => setField("installment_2_amount", parseNum(e.target.value))}
                    />
                  </Field>
                  <Field label="งวดที่ 3 (บาท)">
                    <Input
                      type="number"
                      value={details.installment_3_amount ?? ""}
                      onChange={(e) => setField("installment_3_amount", parseNum(e.target.value))}
                    />
                  </Field>
                  <Field label="ธนาคาร">
                    <Input value={details.bank_name} onChange={(e) => setField("bank_name", e.target.value)} />
                  </Field>
                  <Field label="สาขา">
                    <Input value={details.bank_branch} onChange={(e) => setField("bank_branch", e.target.value)} />
                  </Field>
                  <Field label="ชื่อบัญชี">
                    <Input
                      value={details.bank_account_name}
                      onChange={(e) => setField("bank_account_name", e.target.value)}
                    />
                  </Field>
                  <Field label="เลขที่บัญชี">
                    <Input
                      value={details.bank_account_number}
                      onChange={(e) => setField("bank_account_number", e.target.value)}
                    />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader title="ข้อ 5 — งวดส่งมอบงาน 2 ช่วง" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
                  <Field label="ช่วงที่ 1 — ห้อง/พื้นที่">
                    <Input value={details.phase_1_rooms} onChange={(e) => setField("phase_1_rooms", e.target.value)} />
                  </Field>
                  <Field label="ช่วงที่ 1 — วันที่แล้วเสร็จ">
                    <Input value={details.phase_1_date} onChange={(e) => setField("phase_1_date", e.target.value)} />
                  </Field>
                  <Field label="ช่วงที่ 2 — ห้อง/พื้นที่">
                    <Input value={details.phase_2_rooms} onChange={(e) => setField("phase_2_rooms", e.target.value)} />
                  </Field>
                  <Field label="ช่วงที่ 2 — วันที่แล้วเสร็จ">
                    <Input value={details.phase_2_date} onChange={(e) => setField("phase_2_date", e.target.value)} />
                  </Field>
                  <Field label="เตรียมพื้นที่ล่วงหน้า (วัน)">
                    <Input
                      type="number"
                      value={details.prep_area_days}
                      onChange={(e) => setField("prep_area_days", Number(e.target.value) || 0)}
                    />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader title="พยาน" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-2">
                  <Field label="พยานฝั่งผู้ว่าจ้าง">
                    <Input value={details.witness_1_name} onChange={(e) => setField("witness_1_name", e.target.value)} />
                  </Field>
                  <Field label="พยานฝั่งผู้รับจ้าง">
                    <Input value={details.witness_2_name} onChange={(e) => setField("witness_2_name", e.target.value)} />
                  </Field>
                </div>
              </Card>

              <div className="flex justify-end">
                <Button onClick={() => saveDetails.mutate(details)} disabled={saveDetails.isPending}>
                  {saveDetails.isPending ? <Spinner /> : <Save size={16} />}
                  บันทึก
                </Button>
              </div>
            </div>
          )}
        </>
      )}

      {tab === "attachments" && (
        <div className="space-y-6">
          <datalist id="contract-zone-room-options">
            {roomOptions.map((r) => (
              <option key={r} value={r} />
            ))}
          </datalist>
          {attachmentsQuery.isLoading && (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> กำลังโหลด...
            </div>
          )}
          {pages && pages.length === 0 && (
            <Alert tone="info">ยังไม่มีเอกสารแนบ — กด &quot;เพิ่มหน้าใหม่&quot; แล้ววางรูปภาพที่คัดลอกมา</Alert>
          )}
          {(pages ?? []).map((p) => (
            <Card key={p.key}>
              <CardHeader
                title={p.title}
                right={
                  <button
                    type="button"
                    onClick={() => removePage(p.key)}
                    aria-label="ลบหน้านี้"
                    title="ลบหน้านี้"
                    className="rounded-md p-2 text-slate-400 hover:bg-red-50 hover:text-red-600"
                  >
                    <Trash2 size={16} />
                  </button>
                }
              />
              <div className="space-y-3 p-5">
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
                  <Field label="ชื่อหน้า">
                    <Input value={p.title} onChange={(e) => updatePage(p.key, { title: e.target.value })} />
                  </Field>
                  <Field label="ประเภทรูปแนบ">
                    <Select
                      value={p.attachmentType}
                      onChange={(e) => updatePage(p.key, { attachmentType: e.target.value as AttachmentType })}
                    >
                      {ATTACHMENT_TYPES.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="ชั้น">
                    <Input
                      value={p.floor}
                      onChange={(e) => updatePage(p.key, { floor: e.target.value })}
                      placeholder="เช่น 1st Floor"
                    />
                  </Field>
                  <Field label="ชื่อ Zone" hint={roomOptions.length > 0 ? "พิมพ์เอง หรือเลือกจากชื่อห้องที่เคยบันทึกไว้" : undefined}>
                    <Input
                      list="contract-zone-room-options"
                      value={p.zone}
                      onChange={(e) => updatePage(p.key, { zone: e.target.value })}
                      placeholder="เช่น Living & Dining Area"
                    />
                  </Field>
                </div>
                {p.attachmentType === "furniture_list" && (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <Field label="ลำดับที่เฟอร์นิเจอร์ (สั่งผลิต)" hint="ช่วงเลขรายการในตารางใบราคา เช่น 1-27">
                      <Input
                        value={p.itemRange}
                        onChange={(e) => updatePage(p.key, { itemRange: e.target.value })}
                        placeholder="เช่น 1-27"
                      />
                    </Field>
                    <Field label="อ้างอิงเอกสารแนบท้ายสัญญา" hint="เช่น หน้าตารางใบราคา หรือเลขหน้า (10)">
                      <Input
                        value={p.referenceNote}
                        onChange={(e) => updatePage(p.key, { referenceNote: e.target.value })}
                        placeholder="เช่น (10) หรือ ตารางใบราคาแนบท้าย"
                      />
                    </Field>
                  </div>
                )}
                <ContractAttachmentCanvas
                  ref={(el) => {
                    canvasRefs.current[p.key] = el;
                  }}
                  initialImageUrl={p.imageUrl}
                />
              </div>
            </Card>
          ))}

          <div className="flex items-center justify-between">
            <Button variant="secondary" onClick={addPage}>
              <Plus size={16} /> เพิ่มหน้าใหม่
            </Button>
            <Button onClick={() => saveAttachments.mutate()} disabled={saveAttachments.isPending || !pages}>
              {saveAttachments.isPending ? <Spinner /> : <Save size={16} />}
              บันทึกเอกสารแนบ
            </Button>
          </div>
        </div>
      )}

      {tab === "price" && (
        <div className="space-y-6">
          {house.isLoading && (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> กำลังโหลด...
            </div>
          )}
          {house.data && (house.data.mapping_rows.length ?? 0) === 0 && (
            <Alert tone="warning">บ้านนี้ยังไม่มีข้อมูลราคาที่คำนวณไว้ — ทำขั้นตอน &quot;คำนวณราคา&quot; ให้เสร็จก่อน</Alert>
          )}
          {pricePreview.isLoading && (
            <div className="flex items-center gap-2 text-sm text-slate-500">
              <Spinner /> กำลังคำนวณ...
            </div>
          )}
          {pricePreview.isError && (
            <Alert tone="danger">
              {pricePreview.error instanceof ApiError ? pricePreview.error.message : "โหลดข้อมูลราคาไม่สำเร็จ"}
            </Alert>
          )}

          {priceRows && (
            <>
              <Card>
                <CardHeader title="ตัวเลือกใบราคา" />
                <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-3">
                  <Field label="หักค่ามัดจำออกแบบ" hint="หักออกจากยอดรวม 10DK's work + VAT">
                    <Input
                      type="number"
                      value={depositDeduction || ""}
                      onChange={(e) => setDepositDeduction(Number(e.target.value) || 0)}
                    />
                  </Field>
                  <Field label="หมายเหตุท้าย Grand Total">
                    <Input value={grandTotalNote} onChange={(e) => setGrandTotalNote(e.target.value)} />
                  </Field>
                  <Field label="Remarks (ท้ายเอกสาร)" hint="1 บรรทัด = 1 bullet">
                    <Textarea rows={2} value={remarks} onChange={(e) => setRemarks(e.target.value)} />
                  </Field>
                </div>
              </Card>

              <Card>
                <CardHeader title="รายการเฟอร์นิเจอร์และราคา" description="ข้อมูลนี้จะแนบท้ายเอกสารสัญญาเป็นหน้าสุดท้าย" />
                <div className="p-5">
                  {pricePreview.data && pricePreview.data.warnings.length > 0 && (
                    <div className="mb-4 space-y-2">
                      {pricePreview.data.warnings.map((w, i) => (
                        <Alert key={i} tone="warning">
                          {w}
                        </Alert>
                      ))}
                    </div>
                  )}
                  <QuotationPreviewTable
                    rows={priceRows}
                    onRowChange={updatePriceRow}
                    onRangeChange={updatePriceRange}
                    dkWorkSubtotal={totals.dkWorkSubtotal}
                    purchaseSubtotal={totals.purchaseSubtotal}
                    vat={totals.vat}
                    depositDeduction={depositDeduction}
                    grandTotal={totals.grandTotal}
                    grandTotalNote={grandTotalNote}
                    remarks={remarks}
                  />
                </div>
              </Card>
            </>
          )}
        </div>
      )}
    </div>
  );
}
