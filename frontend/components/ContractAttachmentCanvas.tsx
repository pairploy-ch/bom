"use client";

import { ArrowUpRight, Circle, Eraser, ImageOff, Pencil, Trash2, Type, Undo2, X as XIcon } from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Button, cn } from "@/components/ui/primitives";

// One "เอกสารแนบ" page: paste a screenshot from the clipboard as the
// background, then mark it up — freehand pen, a กากบาท (X) / circle / arrow
// shape stamp (drag to size), a text box, or a spot eraser to remove just
// one mark — all in red to match the reference contract template's own
// X-marks-over-excluded-items convention. Deliberately NOT a general design
// tool (no Fabric.js/Konva) — plain <canvas> + pointer events covers
// everything this tab actually needs.
export interface AttachmentCanvasHandle {
  toBlob: () => Promise<Blob | null>;
  isEmpty: () => boolean;
}

type Tool = "pen" | "cross" | "circle" | "arrow" | "text" | "eraser";

interface Point {
  x: number;
  y: number;
}

type Annotation =
  | { kind: "pen"; points: Point[] }
  | { kind: "cross"; x1: number; y1: number; x2: number; y2: number }
  | { kind: "circle"; x1: number; y1: number; x2: number; y2: number }
  | { kind: "arrow"; x1: number; y1: number; x2: number; y2: number }
  | { kind: "text"; x: number; y: number; text: string };

// The text tool never goes through the drag-to-draw path (see
// handlePointerDown) — the in-progress "draft" shape is always one of these.
type DraggableAnnotation = Extract<Annotation, { kind: "pen" | "cross" | "circle" | "arrow" }>;

const CANVAS_WIDTH = 1000;
const CANVAS_HEIGHT = 700;
const STROKE_COLOR = "#dc2626";
const TEXT_FONT_SIZE = 13;
// How close a click/drag point needs to be to an annotation's line/edge to
// erase it (canvas-space pixels) — generous enough to hit a thin pen stroke
// or shape outline without needing pixel-perfect aim.
const ERASE_RADIUS = 16;

const TOOLS: { key: Tool; label: string; icon: typeof Pencil }[] = [
  { key: "pen", label: "ปากกา", icon: Pencil },
  { key: "cross", label: "กากบาท", icon: XIcon },
  { key: "circle", label: "วงกลม", icon: Circle },
  { key: "arrow", label: "ลูกศร", icon: ArrowUpRight },
  { key: "text", label: "ข้อความ", icon: Type },
  { key: "eraser", label: "ยางลบ", icon: Eraser },
];

function distanceToSegment(p: Point, a: Point, b: Point): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const lengthSq = dx * dx + dy * dy;
  if (lengthSq === 0) return Math.hypot(p.x - a.x, p.y - a.y);
  const t = Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / lengthSq));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

// Point-in/near-annotation hit test for the spot eraser — line/edge-distance
// based for pen strokes and line-shaped marks (cross/arrow), a normalized
// edge-distance for circles, and a padded bounding box for text (measured
// with the canvas's current font so it roughly matches the rendered width).
function hitTestAnnotation(a: Annotation, p: Point, ctx: CanvasRenderingContext2D | null): boolean {
  if (a.kind === "pen") {
    if (a.points.length < 2) {
      const only = a.points[0];
      return !!only && Math.hypot(p.x - only.x, p.y - only.y) <= ERASE_RADIUS;
    }
    for (let i = 0; i < a.points.length - 1; i++) {
      if (distanceToSegment(p, a.points[i], a.points[i + 1]) <= ERASE_RADIUS) return true;
    }
    return false;
  }
  if (a.kind === "cross") {
    return (
      distanceToSegment(p, { x: a.x1, y: a.y1 }, { x: a.x2, y: a.y2 }) <= ERASE_RADIUS ||
      distanceToSegment(p, { x: a.x2, y: a.y1 }, { x: a.x1, y: a.y2 }) <= ERASE_RADIUS
    );
  }
  if (a.kind === "arrow") {
    return distanceToSegment(p, { x: a.x1, y: a.y1 }, { x: a.x2, y: a.y2 }) <= ERASE_RADIUS;
  }
  if (a.kind === "circle") {
    const cx = (a.x1 + a.x2) / 2;
    const cy = (a.y1 + a.y2) / 2;
    const rx = Math.abs(a.x2 - a.x1) / 2;
    const ry = Math.abs(a.y2 - a.y1) / 2;
    if (rx < 1 || ry < 1) return Math.hypot(p.x - cx, p.y - cy) <= ERASE_RADIUS;
    const normDist = Math.hypot((p.x - cx) / rx, (p.y - cy) / ry);
    return Math.abs(normDist - 1) * ((rx + ry) / 2) <= ERASE_RADIUS;
  }
  // text
  const width = ctx ? ctx.measureText(a.text).width : a.text.length * TEXT_FONT_SIZE * 0.55;
  const height = TEXT_FONT_SIZE * 1.2;
  return p.x >= a.x - 4 && p.x <= a.x + width + 4 && p.y >= a.y - 4 && p.y <= a.y + height + 4;
}

function drawShape(ctx: CanvasRenderingContext2D, a: Extract<Annotation, { kind: "cross" | "circle" | "arrow" }>) {
  const { kind, x1, y1, x2, y2 } = a;
  if (kind === "cross") {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.moveTo(x2, y1);
    ctx.lineTo(x1, y2);
    ctx.stroke();
    return;
  }
  if (kind === "circle") {
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    const rx = Math.abs(x2 - x1) / 2;
    const ry = Math.abs(y2 - y1) / 2;
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();
    return;
  }
  // arrow
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const headLen = 16;
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - headLen * Math.cos(angle - Math.PI / 6), y2 - headLen * Math.sin(angle - Math.PI / 6));
  ctx.lineTo(x2 - headLen * Math.cos(angle + Math.PI / 6), y2 - headLen * Math.sin(angle + Math.PI / 6));
  ctx.closePath();
  ctx.fillStyle = STROKE_COLOR;
  ctx.fill();
}

export const ContractAttachmentCanvas = forwardRef<AttachmentCanvasHandle, { initialImageUrl?: string | null }>(
  function ContractAttachmentCanvas({ initialImageUrl }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const baseImageRef = useRef<HTMLImageElement | null>(null);
    const annotationsRef = useRef<Annotation[]>([]);
    const draftRef = useRef<DraggableAnnotation | null>(null);
    const drawingRef = useRef(false);
    // Set while dragging an existing text annotation with the text tool
    // (as opposed to placing a brand-new one) — see handlePointerDown.
    const draggingTextRef = useRef<{
      annotation: Extract<Annotation, { kind: "text" }>;
      offsetX: number;
      offsetY: number;
    } | null>(null);
    // The app's own Thai display font (Kanit, loaded app-wide via next/font)
    // — NOT TP Tankhun. TP Tankhun's license explicitly forbids
    // redistributing the raw .ttf file itself (see
    // backend/app/logic.py's _ensure_thai_fonts_registered docstring), so it
    // can only ever be embedded server-side into generated PDFs, never
    // shipped to the browser for on-canvas rendering here.
    const fontFamilyRef = useRef("sans-serif");
    const [hasImage, setHasImage] = useState(false);
    const [tool, setTool] = useState<Tool>("pen");
    const [textEditor, setTextEditor] = useState<{ cssX: number; cssY: number; canvasX: number; canvasY: number; value: string } | null>(null);

    useEffect(() => {
      fontFamilyRef.current = getComputedStyle(document.documentElement).fontFamily || "sans-serif";
    }, []);

    const redraw = () => {
      const canvas = canvasRef.current;
      const ctx = canvas?.getContext("2d");
      if (!canvas || !ctx) return;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      const img = baseImageRef.current;
      if (img) {
        // "cover" fit — scale up to fill the whole frame and crop the
        // overflow, instead of "contain" (which left white bars around any
        // pasted image whose aspect ratio didn't match the canvas). Anything
        // drawn outside 0..canvas.width/height is clipped by the canvas's
        // own bounds automatically, so no explicit source-rect crop needed.
        const scale = Math.max(canvas.width / img.width, canvas.height / img.height);
        const w = img.width * scale;
        const h = img.height * scale;
        ctx.drawImage(img, (canvas.width - w) / 2, (canvas.height - h) / 2, w, h);
      }

      ctx.strokeStyle = STROKE_COLOR;
      ctx.fillStyle = STROKE_COLOR;
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.font = `${TEXT_FONT_SIZE}px ${fontFamilyRef.current}`;
      ctx.textBaseline = "top";

      const drawOne = (a: Annotation) => {
        if (a.kind === "pen") {
          if (a.points.length < 2) return;
          ctx.beginPath();
          ctx.moveTo(a.points[0].x, a.points[0].y);
          for (const p of a.points.slice(1)) ctx.lineTo(p.x, p.y);
          ctx.stroke();
        } else if (a.kind === "text") {
          ctx.fillText(a.text, a.x, a.y);
        } else {
          drawShape(ctx, a);
        }
      };

      for (const a of annotationsRef.current) drawOne(a);
      if (draftRef.current) drawOne(draftRef.current);
    };

    const loadImage = (src: string) => {
      const img = new Image();
      // Previously-saved pages load their background from the backend API
      // (a different origin/port than the frontend dev server) — without
      // crossOrigin set, drawing that image onto the canvas taints it, and
      // canvas.toBlob() later throws a SecurityError, which is exactly what
      // was causing "Failed to save attachments." after editing an existing
      // page. Freshly-pasted images (blob: URLs) are same-origin and
      // unaffected by this attribute either way.
      img.crossOrigin = "anonymous";
      img.onload = () => {
        baseImageRef.current = img;
        setHasImage(true);
        redraw();
      };
      img.src = src;
    };

    useEffect(() => {
      if (initialImageUrl) loadImage(initialImageUrl);
      // Runs once per mounted page — a page's saved image never changes out
      // from under it after load.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useImperativeHandle(
      ref,
      () => ({
        toBlob: () =>
          new Promise((resolve) => {
            const canvas = canvasRef.current;
            if (!canvas) {
              resolve(null);
              return;
            }
            canvas.toBlob((blob) => resolve(blob), "image/png");
          }),
        isEmpty: () => !baseImageRef.current && annotationsRef.current.length === 0,
      }),
      []
    );

    const posFromEvent = (e: React.PointerEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current!;
      const rect = canvas.getBoundingClientRect();
      return {
        x: ((e.clientX - rect.left) / rect.width) * canvas.width,
        y: ((e.clientY - rect.top) / rect.height) * canvas.height,
      };
    };

    const commitTextEditor = () => {
      setTextEditor((cur) => {
        const value = cur?.value.trim();
        if (cur && value) {
          annotationsRef.current.push({ kind: "text", x: cur.canvasX, y: cur.canvasY, text: value });
          redraw();
        }
        return null;
      });
    };

    // Spot-erases whatever annotation is under `p` — used by the eraser
    // tool, distinct from handleUndo (last action only) and handleClear
    // (everything). ctx.font must already reflect the annotation font for
    // the text hit test's measureText() to be accurate.
    const eraseAtPoint = (p: Point) => {
      const ctx = canvasRef.current?.getContext("2d") ?? null;
      if (ctx) ctx.font = `${TEXT_FONT_SIZE}px ${fontFamilyRef.current}`;
      const before = annotationsRef.current.length;
      annotationsRef.current = annotationsRef.current.filter((a) => !hitTestAnnotation(a, p, ctx));
      if (annotationsRef.current.length !== before) redraw();
    };

    const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (tool === "text") {
        // The canvas itself isn't focusable, so a plain click here would
        // otherwise trigger the browser's default "focus the nearest
        // focusable ancestor" behavior (the tabIndex=0 container below, used
        // for onPaste) — which fires AFTER this handler's setTextEditor
        // causes the <input autoFocus> to mount and grab focus, immediately
        // stealing it back and blurring the input before the user can type
        // a single character. Suppressing the default action here keeps
        // focus on the input; every other tool still wants the container
        // focused on click, so this only applies to the text tool.
        e.preventDefault();
        const canvas = canvasRef.current!;
        const rect = canvas.getBoundingClientRect();
        const pos = posFromEvent(e);

        // Clicking on top of an already-placed text annotation drags it
        // instead of opening a new editor — checked topmost-first so an
        // overlapping later annotation wins.
        const ctx = canvas.getContext("2d");
        if (ctx) ctx.font = `${TEXT_FONT_SIZE}px ${fontFamilyRef.current}`;
        const hit = [...annotationsRef.current]
          .reverse()
          .find((a): a is Extract<Annotation, { kind: "text" }> => a.kind === "text" && hitTestAnnotation(a, pos, ctx));
        if (hit) {
          draggingTextRef.current = { annotation: hit, offsetX: pos.x - hit.x, offsetY: pos.y - hit.y };
          drawingRef.current = true;
          e.currentTarget.setPointerCapture(e.pointerId);
          return;
        }

        setTextEditor({ cssX: e.clientX - rect.left, cssY: e.clientY - rect.top, canvasX: pos.x, canvasY: pos.y, value: "" });
        return;
      }
      if (tool === "eraser") {
        drawingRef.current = true;
        e.currentTarget.setPointerCapture(e.pointerId);
        eraseAtPoint(posFromEvent(e));
        return;
      }
      const pos = posFromEvent(e);
      drawingRef.current = true;
      draftRef.current = tool === "pen" ? { kind: "pen", points: [pos] } : { kind: tool, x1: pos.x, y1: pos.y, x2: pos.x, y2: pos.y };
      e.currentTarget.setPointerCapture(e.pointerId);
      redraw();
    };

    const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
      if (!drawingRef.current) return;
      if (draggingTextRef.current) {
        const pos = posFromEvent(e);
        const drag = draggingTextRef.current;
        drag.annotation.x = pos.x - drag.offsetX;
        drag.annotation.y = pos.y - drag.offsetY;
        redraw();
        return;
      }
      if (tool === "eraser") {
        eraseAtPoint(posFromEvent(e));
        return;
      }
      if (!draftRef.current) return;
      const pos = posFromEvent(e);
      const draft = draftRef.current;
      if (draft.kind === "pen") draft.points.push(pos);
      else {
        draft.x2 = pos.x;
        draft.y2 = pos.y;
      }
      redraw();
    };

    const stopDrawing = () => {
      if (drawingRef.current && draftRef.current) {
        annotationsRef.current.push(draftRef.current);
      }
      drawingRef.current = false;
      draftRef.current = null;
      draggingTextRef.current = null;
      redraw();
    };

    const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const item of Array.from(items)) {
        if (item.type.startsWith("image/")) {
          const file = item.getAsFile();
          if (file) loadImage(URL.createObjectURL(file));
          e.preventDefault();
          return;
        }
      }
    };

    const handleUndo = () => {
      annotationsRef.current.pop();
      redraw();
    };

    const handleClear = () => {
      annotationsRef.current = [];
      redraw();
    };

    return (
      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1">
            {TOOLS.map((t) => {
              const Icon = t.icon;
              return (
                <button
                  key={t.key}
                  type="button"
                  title={t.label}
                  aria-label={t.label}
                  onClick={() => setTool(t.key)}
                  className={cn(
                    "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                    tool === t.key
                      ? "bg-[var(--accent)]/10 text-[var(--accent)]"
                      : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"
                  )}
                >
                  <Icon size={14} />
                  {t.label}
                </button>
              );
            })}
          </div>
          <Button variant="secondary" onClick={handleUndo}>
            <Undo2 size={14} /> ยกเลิกล่าสุด
          </Button>
          <Button variant="secondary" onClick={handleClear}>
            <Trash2 size={14} /> ล้างทั้งหมด
          </Button>
        </div>
        <div
          ref={containerRef}
          tabIndex={0}
          onPaste={handlePaste}
          // Fixed to a large viewport-relative height (not the canvas's
          // native 1000x700 aspect ratio) so a pasted screenshot's whole
          // drawable area fits on screen without scrolling mid-stroke —
          // posFromEvent() already maps CSS-space clicks to canvas-space
          // independently per axis, so stretching here doesn't misalign
          // drawing coordinates. Width is auto (not full-width) so the
          // canvas keeps its own aspect ratio instead of being stretched
          // non-uniformly by the container's width — it's centered via
          // mx-auto on the canvas itself instead.
          className="relative mx-auto h-[75vh] w-fit overflow-hidden rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 outline-none focus:border-[var(--accent)]"
        >
          <canvas
            ref={canvasRef}
            width={CANVAS_WIDTH}
            height={CANVAS_HEIGHT}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={stopDrawing}
            onPointerLeave={stopDrawing}
            className={cn("h-full w-auto touch-none rounded-lg", tool === "text" ? "cursor-text" : "cursor-crosshair")}
          />
          {!hasImage && (
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center gap-2 text-slate-400">
              <ImageOff size={28} />
              <p className="text-sm">คลิกในกรอบนี้แล้วกด Ctrl+V เพื่อวางรูปภาพที่คัดลอกมา</p>
            </div>
          )}
          {textEditor && (
            <input
              autoFocus
              value={textEditor.value}
              onChange={(e) => setTextEditor((cur) => (cur ? { ...cur, value: e.target.value } : cur))}
              onBlur={commitTextEditor}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitTextEditor();
                if (e.key === "Escape") setTextEditor(null);
              }}
              placeholder="พิมพ์ข้อความ..."
              style={{ left: textEditor.cssX, top: textEditor.cssY, fontFamily: fontFamilyRef.current }}
              className="absolute z-10 min-w-[140px] -translate-y-1 rounded border border-[var(--accent)] bg-white/95 px-1.5 py-0.5 text-base text-[#dc2626] shadow outline-none"
            />
          )}
        </div>
      </div>
    );
  }
);
