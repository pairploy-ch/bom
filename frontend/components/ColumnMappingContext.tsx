"use client";

import { createContext, useContext, useState } from "react";
import { ColumnMapping, DEFAULT_COLUMN_MAPPING } from "@/lib/types";

interface Ctx {
  columnMapping: ColumnMapping;
  setColumnMapping: (m: ColumnMapping) => void;
}

const ColumnMappingCtx = createContext<Ctx | null>(null);

// Mirrors the original app's sidebar "Excel Column Mapping" section: it's
// UI-only configuration that lives for the current browser session and
// resets to these defaults on a fresh load — never persisted per-project,
// same as the original (see the feasibility memo, Section 2).
export function ColumnMappingProvider({ children }: { children: React.ReactNode }) {
  const [columnMapping, setColumnMapping] = useState<ColumnMapping>(DEFAULT_COLUMN_MAPPING);
  return (
    <ColumnMappingCtx.Provider value={{ columnMapping, setColumnMapping }}>{children}</ColumnMappingCtx.Provider>
  );
}

export function useColumnMapping(): Ctx {
  const ctx = useContext(ColumnMappingCtx);
  if (!ctx) throw new Error("useColumnMapping must be used inside ColumnMappingProvider");
  return ctx;
}
