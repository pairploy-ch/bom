"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Eye, FileEdit, FileText, FolderOpen, NotebookPen, Upload } from "lucide-react";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { DEFAULT_COLUMN_MAPPING } from "@/lib/types";
import type { QuotationPreview as QuotationPreviewData, QuotationRow } from "@/lib/types";
import { useHouse } from "@/hooks/useHouse";
import { Alert, Button, Card, CardHeader, Field, Input, Select, Spinner, Textarea } from "@/components/ui/primitives";
import { QuotationPreviewTable, quotationTotals } from "@/components/QuotationPreview";
import { useToast } from "@/components/ui/Toast";

type Mode = "house" | "excel";

export default function QuotationPage() {
  const { houseId } = useParams<{ projectId: string; houseId: string }>();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<Mode>("house");

  const [clientName, setClientName] = useState("");
  const [projectName, setProjectName] = useState("");
  const [quotationDate, setQuotationDate] = useState("");
  const [depositDeduction, setDepositDeduction] = useState(0);
  const [remarks, setRemarks] = useState("");
  const [grandTotalNote, setGrandTotalNote] = useState("(ไม่รวมรายการ TBC ค่าขนส่ง, ค่าประกอบและค่าติดตั้ง)");

  // ------------------------------------------------------- company logo --

  const [logoKey, setLogoKey] = useState(0); // bumped to force the <img> to reload after a new upload
  const logoInput = useRef<HTMLInputElement>(null);
  const hasLogo = useQuery({
    queryKey: ["company-logo-check"],
    queryFn: async () => {
      const res = await fetch(api.companyLogoUrl());
      return res.ok;
    },
  });

  const uploadLogo = useMutation({
    mutationFn: (file: File) => api.uploadCompanyLogo(file),
    onSuccess: () => {
      toast.success("อัปเดตโลโก้แล้ว — ใช้กับใบเสนอราคาทุกใบต่อจากนี้");
      setLogoKey((k) => k + 1);
      queryClient.invalidateQueries({ queryKey: ["company-logo-check"] });
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to upload logo."),
  });

  const handleLogoFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) uploadLogo.mutate(file);
  };

  // --------------------------------------------------------- house mode --

  const house = useHouse(houseId);

  const housePreview = useQuery<QuotationPreviewData>({
    queryKey: ["quotation-preview-house", houseId, house.data?.target_sheet_name],
    queryFn: () =>
      api.previewQuotationFromHouse(
        houseId,
        house.data!.target_sheet_name!,
        house.data!.mapping_rows,
        DEFAULT_COLUMN_MAPPING
      ),
    enabled: mode === "house" && !!house.data?.target_sheet_name && (house.data?.mapping_rows.length ?? 0) > 0,
  });

  // --------------------------------------------------------- excel mode --

  const fileInput = useRef<HTMLInputElement>(null);
  const [excelFile, setExcelFile] = useState<File | null>(null);
  const [excelSheetNames, setExcelSheetNames] = useState<string[]>([]);
  const [excelSheetName, setExcelSheetName] = useState("");

  const inspectExcel = useMutation({
    mutationFn: (file: File) => api.inspectQuotationExcel(file),
    onSuccess: (res, file) => {
      setExcelFile(file);
      setExcelSheetNames(res.sheet_names);
      setExcelSheetName(res.sheet_names[0] || "");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to read the Excel file."),
  });

  const excelPreview = useQuery<QuotationPreviewData>({
    queryKey: ["quotation-preview-excel", excelFile?.name, excelSheetName],
    queryFn: () => api.previewQuotationFromExcel(excelFile as File, excelSheetName),
    enabled: mode === "excel" && !!excelFile && !!excelSheetName,
  });

  const handleExcelFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) inspectExcel.mutate(file);
  };

  const preview = mode === "house" ? housePreview : excelPreview;
  const hasSource = mode === "house" ? !!house.data : !!excelFile;

  // ---------------------------------------------------- editable rows --
  // Local copy the user can tweak in place (item name/qty/prices/remark)
  // without needing to go back to the source house or Excel file —
  // re-synced only when a NEW preview result comes in (mode switched, or a
  // new Excel file picked), never overwritten by an in-place edit.
  // Reset-on-prop-change via a render-time comparison (react.dev's
  // recommended pattern), not an effect — an effect here would setState
  // synchronously on every render where preview.data changed, one render
  // late and lint-flagged.
  const [editableRows, setEditableRows] = useState<QuotationRow[] | null>(null);
  const [syncedFrom, setSyncedFrom] = useState<QuotationPreviewData | undefined>(undefined);
  if (preview.data !== syncedFrom) {
    setSyncedFrom(preview.data);
    setEditableRows(preview.data ? preview.data.rows : null);
  }

  const updateRow = (index: number, patch: Partial<QuotationRow>) =>
    setEditableRows((prev) => (prev ? prev.map((r, i) => (i === index ? { ...r, ...patch } : r)) : prev));

  const updateRange = (start: number, end: number, patch: Partial<QuotationRow>) =>
    setEditableRows((prev) =>
      prev ? prev.map((r, i) => (i >= start && i < end ? { ...r, ...patch } : r)) : prev
    );

  const totals = quotationTotals(editableRows ?? [], depositDeduction);

  // -------------------------------------------------------------- export --

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
      api.downloadQuotationPdf({
        client_name: clientName,
        project_name: projectName,
        quotation_date: quotationDate,
        rows: editableRows ?? [],
        deposit_deduction: depositDeduction,
        remarks,
        grand_total_note: grandTotalNote,
      }),
    onSuccess: (blob) => triggerDownload(blob, "ใบเสนอราคา.pdf"),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to generate the PDF."),
  });

  const downloadDocx = useMutation({
    mutationFn: () =>
      api.downloadQuotationDocx({
        client_name: clientName,
        project_name: projectName,
        quotation_date: quotationDate,
        rows: editableRows ?? [],
        deposit_deduction: depositDeduction,
        remarks,
        grand_total_note: grandTotalNote,
      }),
    onSuccess: (blob) => triggerDownload(blob, "ใบเสนอราคา.docx"),
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to generate the Word file."),
  });

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-8">
        <h1 className="flex items-center gap-2 text-2xl font-bold tracking-tight text-slate-900">
          <FileText size={24} /> ใบเสนอราคา
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          ดึงข้อมูลราคาที่คำนวณไว้แล้วมาตัดคอลัมน์ภายในออก แก้ไขได้ในตาราง แล้ว export เป็น PDF หรือ Word สำหรับส่งลูกค้า
        </p>
      </header>

      <Card className="mb-6">
        <CardHeader
          icon={<Building2 size={16} />}
          title="โลโก้บริษัท"
          description="อัปโหลดครั้งเดียว — ใช้กับใบเสนอราคาทุกใบที่ export ต่อจากนี้"
        />
        <div className="flex items-center gap-4 p-5">
          {hasLogo.data ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={logoKey}
              src={api.companyLogoUrl()}
              alt="Company logo"
              className="h-16 rounded border border-slate-200 bg-white object-contain px-2"
            />
          ) : (
            <div className="flex h-16 w-28 items-center justify-center rounded border border-dashed border-slate-300 text-xs text-slate-400">
              ไม่มีโลโก้
            </div>
          )}
          <div>
            <input
              ref={logoInput}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={handleLogoFile}
              disabled={uploadLogo.isPending}
            />
            <Button variant="secondary" onClick={() => logoInput.current?.click()} disabled={uploadLogo.isPending}>
              {uploadLogo.isPending && <Spinner />}
              {hasLogo.data ? "เปลี่ยนโลโก้" : "อัปโหลดโลโก้"}
            </Button>
          </div>
        </div>
      </Card>

      <Card className="mb-6">
        <nav className="flex gap-1 border-b border-slate-200 px-2 pt-2">
          {(
            [
              { key: "house" as const, label: "บ้านนี้", icon: FolderOpen },
              { key: "excel" as const, label: "อัปโหลดไฟล์ Excel", icon: Upload },
            ]
          ).map((tab) => (
            <button
              key={tab.key}
              onClick={() => setMode(tab.key)}
              className={
                "flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors " +
                (mode === tab.key
                  ? "border-indigo-600 text-indigo-600"
                  : "border-transparent text-slate-500 hover:text-slate-800")
              }
            >
              <tab.icon size={16} />
              {tab.label}
            </button>
          ))}
        </nav>

        <div className="space-y-4 p-5">
          {mode === "house" && (
            <>
              {house.isLoading && (
                <div className="flex items-center gap-2 text-sm text-slate-500">
                  <Spinner /> กำลังโหลดข้อมูลบ้าน…
                </div>
              )}
              {house.isError && <Alert tone="danger">โหลดข้อมูลบ้านนี้ไม่สำเร็จ</Alert>}
              {house.data && (house.data.mapping_rows.length ?? 0) === 0 && (
                <Alert tone="warning">บ้านนี้ยังไม่มีข้อมูลราคาที่คำนวณไว้ — ทำ Step 1-3 ให้เสร็จก่อน</Alert>
              )}
            </>
          )}

          {mode === "excel" && (
            <div className="space-y-3">
              <label className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 px-6 py-8 text-center hover:border-indigo-300 hover:bg-indigo-50/40">
                <input
                  ref={fileInput}
                  type="file"
                  accept=".xlsx"
                  className="hidden"
                  onChange={handleExcelFile}
                  disabled={inspectExcel.isPending}
                />
                {inspectExcel.isPending ? (
                  <>
                    <Spinner className="text-indigo-600" />
                    <span className="text-sm text-slate-500">กำลังอ่านไฟล์…</span>
                  </>
                ) : (
                  <>
                    <Upload size={32} className="text-slate-400" />
                    <span className="text-sm font-medium text-slate-700">
                      คลิกเพื่ออัปโหลดไฟล์ Excel ที่ export มาแล้ว (.xlsx)
                    </span>
                    <span className="text-xs text-slate-400">
                      แก้ไขค่าในไฟล์ได้ตามใจ แต่อย่าสลับ/ลบแถวหรือคอลัมน์ — ถ้าไฟล์มีสูตรที่ยังไม่เคยคำนวณ
                      ให้เปิดด้วย Excel แล้วบันทึกก่อนอัปโหลด
                    </span>
                  </>
                )}
              </label>
              {excelFile && (
                <p className="text-sm text-slate-600">
                  ไฟล์: <strong>{excelFile.name}</strong>
                </p>
              )}
              {excelSheetNames.length > 0 && (
                <Field label="Sheet">
                  <Select value={excelSheetName} onChange={(e) => setExcelSheetName(e.target.value)}>
                    {excelSheetNames.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
            </div>
          )}
        </div>
      </Card>

      {hasSource && (
        <Card className="mb-6">
          <CardHeader icon={<NotebookPen size={16} />} title="ข้อมูลใบเสนอราคา" />
          <div className="grid grid-cols-1 gap-4 p-5 sm:grid-cols-3">
            <Field label="ชื่อลูกค้า">
              <Input value={clientName} onChange={(e) => setClientName(e.target.value)} />
            </Field>
            <Field label="ชื่อโปรเจกต์ / ที่อยู่">
              <Input value={projectName} onChange={(e) => setProjectName(e.target.value)} />
            </Field>
            <Field label="วันที่">
              <Input
                value={quotationDate}
                onChange={(e) => setQuotationDate(e.target.value)}
                placeholder="e.g. 16 March 2026"
              />
            </Field>
            <Field label="หักค่ามัดจำออกแบบ" hint="หักออกจากยอดรวม 10DK's work + VAT">
              <Input
                type="number"
                value={depositDeduction || ""}
                onChange={(e) => setDepositDeduction(Number(e.target.value) || 0)}
              />
            </Field>
            <Field label="หมายเหตุท้าย Grand Total" hint="ข้อความเล็กๆ ต่อท้ายยอดรวมสุทธิ">
              <Input value={grandTotalNote} onChange={(e) => setGrandTotalNote(e.target.value)} />
            </Field>
            <Field
              label="Remarks (ท้ายเอกสาร)"
              hint="1 บรรทัด = 1 bullet — ครอบข้อความด้วย **...** เพื่อทำตัวหนา+ขีดเส้นใต้เฉพาะส่วนนั้น เช่น ราคาดังกล่าว **ไม่รวมฟูกที่นอน**"
            >
              <Textarea rows={2} value={remarks} onChange={(e) => setRemarks(e.target.value)} />
            </Field>
          </div>
        </Card>
      )}

      {preview.isLoading && (
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Spinner /> กำลังโหลด...
        </div>
      )}
      {preview.isError && (
        <Alert tone="danger">{preview.error instanceof ApiError ? preview.error.message : "โหลดข้อมูลไม่สำเร็จ"}</Alert>
      )}

      {editableRows && (
        <Card>
          <CardHeader
            icon={<Eye size={16} />}
            title="Preview ใบเสนอราคา"
            description="แก้ไขค่าในตารางได้โดยตรงก่อน export"
            right={
              <div className="flex gap-2">
                <Button variant="secondary" onClick={() => downloadDocx.mutate()} disabled={downloadDocx.isPending}>
                  {downloadDocx.isPending ? <Spinner /> : <FileEdit size={16} />}
                  Export Word
                </Button>
                <Button onClick={() => downloadPdf.mutate()} disabled={downloadPdf.isPending}>
                  {downloadPdf.isPending ? <Spinner /> : <FileText size={16} />}
                  Export PDF
                </Button>
              </div>
            }
          />
          <div className="space-y-4 p-5">
            {preview.data && preview.data.warnings.length > 0 && (
              <div className="space-y-2">
                {preview.data.warnings.map((w, i) => (
                  <Alert key={i} tone="warning">
                    {w}
                  </Alert>
                ))}
              </div>
            )}
            <QuotationPreviewTable
              rows={editableRows}
              onRowChange={updateRow}
              onRangeChange={updateRange}
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
      )}
    </div>
  );
}
