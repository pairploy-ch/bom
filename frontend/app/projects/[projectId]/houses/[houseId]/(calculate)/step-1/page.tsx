"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FileUp } from "lucide-react";
import { useParams } from "next/navigation";
import { useRef } from "react";
import { api, ApiError } from "@/lib/api";
import { useHouse, houseKey } from "@/hooks/useHouse";
import { Alert, Card, CardHeader, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

export default function Step1Page() {
  const { houseId } = useParams<{ houseId: string }>();
  const house = useHouse(houseId);
  const queryClient = useQueryClient();
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);

  const upload = useMutation({
    mutationFn: (file: File) => api.uploadTemplate(houseId, file),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: houseKey(houseId) });
      toast.success(`Loaded '${res.excel_filename}' — sheets found: ${res.sheet_names.join(", ")}`);
      if (fileInput.current) fileInput.current.value = "";
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to upload template."),
  });

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) upload.mutate(file);
  };

  return (
    <Card>
      <CardHeader title="Step 1 — Upload Excel BOM Template" />
      <div className="space-y-4 p-5">
        <label className="flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 px-6 py-10 text-center hover:border-indigo-300 hover:bg-indigo-50/40">
          <input
            ref={fileInput}
            type="file"
            accept=".xlsx"
            className="hidden"
            onChange={handleFile}
            disabled={upload.isPending}
          />
          {upload.isPending ? (
            <>
              <Spinner className="text-indigo-600" />
              <span className="text-sm text-slate-500">Uploading…</span>
            </>
          ) : (
            <>
              <FileUp size={32} className="text-slate-400" />
              <span className="text-sm font-medium text-slate-700">Click to upload the Excel template (.xlsx)</span>
              <span className="text-xs text-slate-400">Formulas and formatting are preserved</span>
            </>
          )}
        </label>

        {house.data?.has_template && (
          <Alert tone="success">
            Current template on file: <strong>{house.data.excel_filename}</strong> — sheets:{" "}
            {house.data.sheet_names.join(", ")}
          </Alert>
        )}
      </div>
    </Card>
  );
}
