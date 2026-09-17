"use client";

import {
  ArrowUpRight,
  Circle,
  Eraser,
  ImageOff,
  Pencil,
  Square,
  Trash2,
  Type,
  Undo2,
  X as XIcon,
} from "lucide-react";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import { Button, cn } from "@/components/ui/primitives";

// One "เอกสารแนบ" page: paste a screenshot from the clipboard as the
// background, then mark it up — freehand pen, a กากบาท (X) / circle /
// rectangle / arrow shape stamp (drag to size), a text box, or a spot eraser
// to remove just one mark — in either red (to match the reference contract
// template's own X-marks-over-excluded-items convention) or white (for dark
// photo backgrounds red doesn't show up against). Deliberately NOT a general
// design tool (no Fabric.js/Konva) — plain <canvas> + pointer events covers
// everything this tab actually needs.
export interface AttachmentCanvasHandle {
  toBlob: () => Promise<Blob | null>;
  isEmpty: () => boolean;
}

type Tool = "pen" | "cross" | "circle" | "rect" | "arrow" | "text" | "eraser";

interface Point {
  x: number;
  y: number;
}

// Every annotation carries its own `color`, fixed at the moment it was
// drawn — switching the active color afterwards only affects new marks, not
// ones already on the page.
type Annotation =
  | { kind: "pen"; points: Point[]; color: string }
  | { kind: "cross"; x1: number; y1: number; x2: number; y2: number; color: string }
  | { kind: "circle"; x1: number; y1: number; x2: number; y2: number; color: string }
  | { kind: "rect"; x1: number; y1: number; x2: number; y2: number; color: string; filled: boolean }
  | { kind: "arrow"; x1: number; y1: number; x2: number; y2: number; color: string }
  | { kind: "text"; x: number; y: number; text: string; color: string };

// The text tool never goes through the drag-to-draw path (see
// handlePointerDown) — the in-progress "draft" shape is always one of these.
type DraggableAnnotation = Extract<Annotation, { kind: "pen" | "cross" | "circle" | "rect" | "arrow" }>;

const STROKE_COLORS = [
  { value: "#dc2626", label: "แดง" },
  { value: "#ffffff", label: "ขาว" },
];

// Starting size only — the frame itself is freely resizable by dragging its
// bottom-right corner (see the resize handle), so these are just what a
// brand-new page opens at, not a hard cap.
const DEFAULT_CANVAS_WIDTH = 1000;
const DEFAULT_CANVAS_HEIGHT = 700;
// Bounds for that drag-resize — generous enough for a tall 2-image page
// without allowing something absurd that would blow up memory/export time.
const MIN_CANVAS_WIDTH = 400;
const MIN_CANVAS_HEIGHT = 300;
const MAX_CANVAS_WIDTH = 2400;
const MAX_CANVAS_HEIGHT = 3200;
// A fresh switch to 2-image layout bumps the frame to at least this tall
// (never shrinks it) so each half doesn't start out cramped at half of the
// single-image default height.
const TWO_IMAGE_MIN_HEIGHT = 1000;

const DEFAULT_STROKE_COLOR = STROKE_COLORS[0].value;
const TEXT_FONT_SIZE = 13;
// How close a click/drag point needs to be to an annotation's line/edge to
// erase it (canvas-space pixels) — generous enough to hit a thin pen stroke
// or shape outline without needing pixel-perfect aim.
const ERASE_RADIUS = 16;

// In 2-image layout, how far the top/bottom split can be dragged — never
// all the way to 0/1, so neither slot can be resized down to nothing.
const MIN_SPLIT_RATIO = 0.15;
const MAX_SPLIT_RATIO = 0.85;

// In 2-image layout, how narrow either slot's own image box can be dragged
// relative to the full frame width — the two don't have to match each
// other, each has its own independent ratio.
const MIN_SLOT_WIDTH_RATIO = 0.25;
const MAX_SLOT_WIDTH_RATIO = 1;

function clamp(v: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, v));
}

const TOOLS: { key: Tool; label: string; icon: typeof Pencil }[] = [
  { key: "pen", label: "ปากกา", icon: Pencil },
  { key: "cross", label: "กากบาท", icon: XIcon },
  { key: "circle", label: "วงกลม", icon: Circle },
  { key: "rect", label: "สี่เหลี่ยม", icon: Square },
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
  if (a.kind === "rect") {
    if (a.filled) {
      return (
        p.x >= Math.min(a.x1, a.x2) &&
        p.x <= Math.max(a.x1, a.x2) &&
        p.y >= Math.min(a.y1, a.y2) &&
        p.y <= Math.max(a.y1, a.y2)
      );
    }
    const corners = [
      { x: a.x1, y: a.y1 },
      { x: a.x2, y: a.y1 },
      { x: a.x2, y: a.y2 },
      { x: a.x1, y: a.y2 },
    ];
    for (let i = 0; i < 4; i++) {
      if (distanceToSegment(p, corners[i], corners[(i + 1) % 4]) <= ERASE_RADIUS) return true;
    }
    return false;
  }
  // text
  const width = ctx ? ctx.measureText(a.text).width : a.text.length * TEXT_FONT_SIZE * 0.55;
  const height = TEXT_FONT_SIZE * 1.2;
  return p.x >= a.x - 4 && p.x <= a.x + width + 4 && p.y >= a.y - 4 && p.y <= a.y + height + 4;
}

// Caller sets ctx.strokeStyle/fillStyle to the annotation's own `color`
// before calling this (see redraw's drawOne) — kept out of here since the
// text-drawing path (in redraw, not this function) needs the same color set
// on ctx too.
function drawShape(ctx: CanvasRenderingContext2D, a: Extract<Annotation, { kind: "cross" | "circle" | "rect" | "arrow" }>) {
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
  if (kind === "rect") {
    const rx = Math.min(x1, x2);
    const ry = Math.min(y1, y2);
    const rw = Math.abs(x2 - x1);
    const rh = Math.abs(y2 - y1);
    if (a.filled) ctx.fillRect(rx, ry, rw, rh);
    else ctx.strokeRect(rx, ry, rw, rh);
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
  ctx.fill();
}

// Remaps an annotation's coordinates by (sx, sy) — used when the frame is
// resized (drag-corner or the auto height bump on switching to 2-image
// layout) so existing marks stay in the same relative spot on the image
// instead of keeping their old absolute pixel position.
function scaleAnnotation(a: Annotation, sx: number, sy: number): Annotation {
  if (a.kind === "pen") return { ...a, points: a.points.map((p) => ({ x: p.x * sx, y: p.y * sy })) };
  if (a.kind === "text") return { ...a, x: a.x * sx, y: a.y * sy };
  return { ...a, x1: a.x1 * sx, y1: a.y1 * sy, x2: a.x2 * sx, y2: a.y2 * sy };
}

export const ContractAttachmentCanvas = forwardRef<AttachmentCanvasHandle, { initialImageUrl?: string | null }>(
  function ContractAttachmentCanvas({ initialImageUrl }, ref) {
    const containerRef = useRef<HTMLDivElement>(null);
    const canvasRef = useRef<HTMLCanvasElement>(null);
    // 1 slot (the whole frame) or 2 (stacked top/bottom halves) — which
    // slot a paste lands in is tracked by activeSlotRef, set from the y
    // position of whatever was last clicked/dragged on the canvas (see
    // handlePointerDown), so "click the area you want, then Ctrl+V" keeps
    // working exactly as before, just per-half instead of whole-frame.
    const imageSlotsRef = useRef<(HTMLImageElement | null)[]>([null]);
    const activeSlotRef = useRef(0);
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
    const [layout, setLayout] = useState<1 | 2>(1);
    const [filledSlots, setFilledSlots] = useState<boolean[]>([false]);
    // Fraction of the frame's total height given to the top slot in
    // 2-image layout — dragging the divider between the two adjusts this
    // instead of resizing the frame itself, so the overall page height
    // stays fixed and only how it's divided between the two photos changes.
    const [splitRatio, setSplitRatio] = useState(0.5);
    const resizingSplitRef = useRef(false);
    // Each slot's own image-box width as a fraction of the full frame width
    // (independent of the other slot's — the top image can be narrower than
    // the bottom one or vice versa). Always centered horizontally within
    // the frame. Index 0 is unused/ignored in 1-image layout (that slot
    // always spans the full width).
    const [slotWidthRatios, setSlotWidthRatios] = useState<[number, number]>([1, 1]);
    const slotWidthResizeRef = useRef<number | null>(null);
    // The frame's own width/height — no longer a fixed constant, since it's
    // now resizable by dragging its bottom-right corner (see the resize
    // handle / handleFrameResize*). Drag state (start point + a snapshot of
    // the annotations to rescale from) lives in a ref since it only matters
    // between pointerdown and pointerup, never needs to trigger a render.
    const [canvasSize, setCanvasSize] = useState({ w: DEFAULT_CANVAS_WIDTH, h: DEFAULT_CANVAS_HEIGHT });
    const frameResizeRef = useRef<{
      startX: number;
      startY: number;
      startW: number;
      startH: number;
      baseAnnotations: Annotation[];
    } | null>(null);
    const [tool, setTool] = useState<Tool>("pen");
    const [strokeColor, setStrokeColor] = useState(DEFAULT_STROKE_COLOR);
    // Only meaningful for the rect tool — a solid block in the current color
    // instead of just an outline (e.g. for redacting/highlighting an area).
    const [fillRect, setFillRect] = useState(false);
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

      // Layout 1 = one slot covering the whole frame; layout 2 = two slots
      // stacked top/bottom, split at splitRatio (total height stays fixed —
      // dragging the divider only changes how it's divided between them).
      const splitY = layout === 2 ? canvas.height * splitRatio : canvas.height;
      const slotBounds = layout === 2 ? [
        { y: 0, h: splitY },
        { y: splitY, h: canvas.height - splitY },
      ] : [{ y: 0, h: canvas.height }];
      imageSlotsRef.current.forEach((img, i) => {
        if (!img) return;
        const { y: slotY, h: slotHeight } = slotBounds[i];
        // Each slot's own box width, centered — only meaningful in 2-image
        // layout (layout 1's single slot always spans the full width).
        const widthRatio = layout === 2 ? slotWidthRatios[i] : 1;
        const boxWidth = canvas.width * widthRatio;
        const boxX = (canvas.width - boxWidth) / 2;
        // "cover" fit within this slot's box — scale up to fill it and crop
        // the overflow, instead of "contain" (which left white bars around
        // any pasted image whose aspect ratio didn't match the box). Clipped
        // to the slot's own rect so an oversized image can't bleed into the
        // other slot or outside a narrowed box.
        const scale = Math.max(boxWidth / img.width, slotHeight / img.height);
        const w = img.width * scale;
        const h = img.height * scale;
        ctx.save();
        ctx.beginPath();
        ctx.rect(boxX, slotY, boxWidth, slotHeight);
        ctx.clip();
        ctx.drawImage(img, boxX + (boxWidth - w) / 2, slotY + (slotHeight - h) / 2, w, h);
        ctx.restore();
        if (layout === 2 && widthRatio < 1) {
          ctx.save();
          ctx.strokeStyle = "#cbd5e1";
          ctx.lineWidth = 1;
          ctx.strokeRect(boxX, slotY, boxWidth, slotHeight);
          ctx.restore();
        }
      });
      if (layout === 2) {
        ctx.save();
        ctx.strokeStyle = "#cbd5e1";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, splitY);
        ctx.lineTo(canvas.width, splitY);
        ctx.stroke();
        ctx.restore();
      }

      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.font = `${TEXT_FONT_SIZE}px ${fontFamilyRef.current}`;
      ctx.textBaseline = "top";

      const drawOne = (a: Annotation) => {
        ctx.strokeStyle = a.color;
        ctx.fillStyle = a.color;
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

    const loadImageIntoSlot = (index: number, src: string) => {
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
        imageSlotsRef.current[index] = img;
        setFilledSlots((cur) => {
          const next = [...cur];
          next[index] = true;
          return next;
        });
        redraw();
      };
      img.src = src;
    };

    // Adding/removing the second slot never touches slot 0's image — only
    // the split point (redraw's slotHeight) changes. The actual redraw
    // happens in the effect below, keyed on `layout` — calling redraw()
    // right here would still close over the pre-update `layout` state
    // (setState doesn't apply until the next render).
    const changeLayout = (n: 1 | 2) => {
      setLayout(n);
      if (n === 2 && imageSlotsRef.current.length < 2) {
        imageSlotsRef.current.push(null);
        setFilledSlots((cur) => [cur[0] ?? false, false]);
        setSplitRatio(0.5);
        // Give each half a decent starting height instead of splitting
        // whatever single-image height was already set (often the cramped
        // default) — never shrinks an already-taller frame.
        if (canvasSize.h < TWO_IMAGE_MIN_HEIGHT) {
          const sy = TWO_IMAGE_MIN_HEIGHT / canvasSize.h;
          annotationsRef.current = annotationsRef.current.map((a) => scaleAnnotation(a, 1, sy));
          setCanvasSize({ w: canvasSize.w, h: TWO_IMAGE_MIN_HEIGHT });
        }
      } else if (n === 1 && imageSlotsRef.current.length > 1) {
        imageSlotsRef.current = [imageSlotsRef.current[0]];
        setFilledSlots((cur) => [cur[0] ?? false]);
        activeSlotRef.current = 0;
      }
    };

    // Drag-resizes the whole frame from its bottom-right corner. Annotation
    // coordinates are rescaled from a snapshot taken at drag-start (not
    // incrementally per pointermove) so repeated small scale-and-round-trip
    // errors can't accumulate during one drag.
    const handleFrameResizeDown = (e: React.PointerEvent<HTMLDivElement>) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      frameResizeRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        startW: canvas.width,
        startH: canvas.height,
        baseAnnotations: annotationsRef.current,
      };
      e.currentTarget.setPointerCapture(e.pointerId);
      e.preventDefault();
    };

    const handleFrameResizeMove = (e: React.PointerEvent<HTMLDivElement>) => {
      const start = frameResizeRef.current;
      if (!start) return;
      const newW = clamp(start.startW + (e.clientX - start.startX), MIN_CANVAS_WIDTH, MAX_CANVAS_WIDTH);
      const newH = clamp(start.startH + (e.clientY - start.startY), MIN_CANVAS_HEIGHT, MAX_CANVAS_HEIGHT);
      const sx = newW / start.startW;
      const sy = newH / start.startH;
      annotationsRef.current = start.baseAnnotations.map((a) => scaleAnnotation(a, sx, sy));
      setCanvasSize({ w: newW, h: newH });
    };

    const handleFrameResizeUp = (e: React.PointerEvent<HTMLDivElement>) => {
      frameResizeRef.current = null;
      e.currentTarget.releasePointerCapture(e.pointerId);
    };

    const clearSlot = (index: number) => {
      imageSlotsRef.current[index] = null;
      setFilledSlots((cur) => {
        const next = [...cur];
        next[index] = false;
        return next;
      });
      redraw();
    };

    useEffect(() => {
      if (initialImageUrl) loadImageIntoSlot(0, initialImageUrl);
      // Runs once per mounted page — a page's saved image never changes out
      // from under it after load.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Redraws whenever the slot split or frame size changes — the canvas's
    // width/height attributes (set from canvasSize below) only take effect
    // in the DOM after this render commits, so a redraw() called inline
    // during the event handler that changed them would still see the old
    // canvas.width/height. This effect runs after that commit instead.
    useEffect(() => {
      redraw();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [layout, splitRatio, canvasSize, slotWidthRatios]);

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
        isEmpty: () => imageSlotsRef.current.every((img) => !img) && annotationsRef.current.length === 0,
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
          annotationsRef.current.push({ kind: "text", x: cur.canvasX, y: cur.canvasY, text: value, color: strokeColor });
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
      // Whichever half was clicked becomes the paste target (see
      // handlePaste) — updated on every click regardless of tool, since it's
      // just bookkeeping and never interferes with drawing.
      if (layout === 2) {
        const rect = canvasRef.current!.getBoundingClientRect();
        activeSlotRef.current = e.clientY - rect.top < rect.height * splitRatio ? 0 : 1;
      }
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
      draftRef.current =
        tool === "pen"
          ? { kind: "pen", points: [pos], color: strokeColor }
          : tool === "rect"
            ? { kind: "rect", x1: pos.x, y1: pos.y, x2: pos.x, y2: pos.y, color: strokeColor, filled: fillRect }
            : { kind: tool, x1: pos.x, y1: pos.y, x2: pos.x, y2: pos.y, color: strokeColor };
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
          if (file) loadImageIntoSlot(activeSlotRef.current, URL.createObjectURL(file));
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

    // Dragging the divider between the two slots resizes them (total frame
    // height is fixed — only how it's split between top/bottom changes).
    // Uses the container (not the canvas) for its rect since the handle
    // sits in CSS/percentage space, not canvas-pixel space.
    const handleDividerPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
      resizingSplitRef.current = true;
      e.currentTarget.setPointerCapture(e.pointerId);
      e.preventDefault();
    };

    const handleDividerPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
      if (!resizingSplitRef.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const ratio = (e.clientY - rect.top) / rect.height;
      setSplitRatio(Math.min(MAX_SPLIT_RATIO, Math.max(MIN_SPLIT_RATIO, ratio)));
    };

    const handleDividerPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
      resizingSplitRef.current = false;
      e.currentTarget.releasePointerCapture(e.pointerId);
    };

    // Dragging a slot's own width handle (its box's right edge) resizes
    // just that slot — the box always stays centered, so the ratio is
    // derived directly from how far the pointer is from the frame's
    // horizontal center, not from an incremental delta.
    const handleSlotWidthPointerDown = (index: number) => (e: React.PointerEvent<HTMLDivElement>) => {
      slotWidthResizeRef.current = index;
      e.currentTarget.setPointerCapture(e.pointerId);
      e.preventDefault();
    };

    const handleSlotWidthPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
      const index = slotWidthResizeRef.current;
      if (index === null || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const halfWidthRatio = Math.abs((e.clientX - rect.left) / rect.width - 0.5);
      const ratio = clamp(halfWidthRatio * 2, MIN_SLOT_WIDTH_RATIO, MAX_SLOT_WIDTH_RATIO);
      setSlotWidthRatios((cur) => {
        const next: [number, number] = [...cur];
        next[index] = ratio;
        return next;
      });
    };

    const handleSlotWidthPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
      slotWidthResizeRef.current = null;
      e.currentTarget.releasePointerCapture(e.pointerId);
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
          <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1.5">
            {STROKE_COLORS.map((c) => (
              <button
                key={c.value}
                type="button"
                title={c.label}
                aria-label={c.label}
                onClick={() => setStrokeColor(c.value)}
                style={{ backgroundColor: c.value }}
                className={cn(
                  "h-5 w-5 rounded-full border-2 transition-colors",
                  strokeColor === c.value ? "border-[var(--accent)]" : "border-slate-300"
                )}
              />
            ))}
          </div>
          {tool === "rect" && (
            <button
              type="button"
              onClick={() => setFillRect((v) => !v)}
              className={cn(
                "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition-colors",
                fillRect
                  ? "border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)]"
                  : "border-slate-200 bg-white text-slate-500 hover:bg-slate-100 hover:text-slate-900"
              )}
            >
              <Square size={14} fill={fillRect ? "currentColor" : "none"} /> พื้นทึบ
            </button>
          )}
          <Button variant="secondary" onClick={handleUndo}>
            <Undo2 size={14} /> ยกเลิกล่าสุด
          </Button>
          <Button variant="secondary" onClick={handleClear}>
            <Trash2 size={14} /> ล้างทั้งหมด
          </Button>
          <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1">
            {([1, 2] as const).map((n) => (
              <button
                key={n}
                type="button"
                title={n === 1 ? "วางรูปเดียวเต็มกรอบ" : "แบ่งบน-ล่าง วางได้ 2 รูป"}
                onClick={() => changeLayout(n)}
                className={cn(
                  "rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors",
                  layout === n
                    ? "bg-[var(--accent)]/10 text-[var(--accent)]"
                    : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"
                )}
              >
                {n === 1 ? "1 รูป" : "2 รูป (บน-ล่าง)"}
              </button>
            ))}
          </div>
          {layout === 2 && (
            <>
              <Button variant="secondary" onClick={() => clearSlot(0)} disabled={!filledSlots[0]}>
                <XIcon size={14} /> ล้างรูปบน
              </Button>
              <Button variant="secondary" onClick={() => clearSlot(1)} disabled={!filledSlots[1]}>
                <XIcon size={14} /> ล้างรูปล่าง
              </Button>
            </>
          )}
        </div>
        {/* Scrolls in whichever direction the frame ends up bigger than the
            viewport — the frame's own size is no longer capped to a fraction
            of the viewport height, so it can genuinely grow past it. */}
        <div className="max-w-full overflow-auto rounded-lg">
        <div
          ref={containerRef}
          tabIndex={0}
          onPaste={handlePaste}
          style={{ width: canvasSize.w, height: canvasSize.h }}
          // posFromEvent() maps CSS-space clicks to canvas-space
          // independently per axis, so the container/canvas being sized in
          // real px (1:1 with the canvas's own resolution, not stretched to
          // fit some fraction of the viewport) doesn't misalign drawing
          // coordinates — it just means what you draw is always full-res.
          className="relative overflow-hidden rounded-lg border-2 border-dashed border-slate-300 bg-slate-50 outline-none focus:border-[var(--accent)]"
        >
          <canvas
            ref={canvasRef}
            width={canvasSize.w}
            height={canvasSize.h}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={stopDrawing}
            onPointerLeave={stopDrawing}
            className={cn("block touch-none rounded-lg", tool === "text" ? "cursor-text" : "cursor-crosshair")}
          />
          <div
            onPointerDown={handleFrameResizeDown}
            onPointerMove={handleFrameResizeMove}
            onPointerUp={handleFrameResizeUp}
            title="ลากเพื่อปรับขนาดกรอบ (กว้าง/สูง)"
            className="absolute bottom-0 right-0 z-30 h-6 w-6 cursor-nwse-resize touch-none rounded-tl-md bg-[var(--accent)]/70 hover:bg-[var(--accent)]"
          />
          {layout === 2 && (
            <div
              onPointerDown={handleDividerPointerDown}
              onPointerMove={handleDividerPointerMove}
              onPointerUp={handleDividerPointerUp}
              style={{ top: `${splitRatio * 100}%` }}
              className="absolute inset-x-0 z-20 -mt-2 h-4 cursor-row-resize touch-none"
              title="ลากเพื่อปรับสัดส่วนรูปบน/ล่าง"
            >
              <div className="mx-auto mt-1.5 h-1 w-16 rounded-full bg-[var(--accent)]/70" />
            </div>
          )}
          {layout === 2 &&
            ([0, 1] as const).map((i) => {
              const slotTop = i === 0 ? 0 : splitRatio * 100;
              const slotHeight = i === 0 ? splitRatio * 100 : (1 - splitRatio) * 100;
              const rightEdgePct = (1 + slotWidthRatios[i]) / 2 * 100;
              return (
                <div
                  key={i}
                  onPointerDown={handleSlotWidthPointerDown(i)}
                  onPointerMove={handleSlotWidthPointerMove}
                  onPointerUp={handleSlotWidthPointerUp}
                  style={{ left: `${rightEdgePct}%`, top: `${slotTop}%`, height: `${slotHeight}%` }}
                  className="absolute z-20 -ml-2 flex w-4 cursor-col-resize touch-none items-center justify-center"
                  title={i === 0 ? "ลากเพื่อปรับความกว้างรูปบน" : "ลากเพื่อปรับความกว้างรูปล่าง"}
                >
                  <div className="h-4 w-1 rounded-full bg-[var(--accent)]/70" />
                </div>
              );
            })}
          {Array.from({ length: layout }, (_, i) => i)
            .filter((i) => !filledSlots[i])
            .map((i) => {
              const top = layout === 1 ? 0 : i === 0 ? 0 : splitRatio * 100;
              const height = layout === 1 ? 100 : i === 0 ? splitRatio * 100 : (1 - splitRatio) * 100;
              return (
                <div
                  key={i}
                  style={{ top: `${top}%`, height: `${height}%` }}
                  className="pointer-events-none absolute inset-x-0 flex flex-col items-center justify-center gap-2 text-slate-400"
                >
                  <ImageOff size={layout === 1 ? 28 : 20} />
                  <p className="text-sm">
                    คลิก{layout === 2 ? (i === 0 ? "ครึ่งบน" : "ครึ่งล่าง") : "ในกรอบนี้"}แล้วกด Ctrl+V เพื่อวางรูปภาพที่คัดลอกมา
                  </p>
                </div>
              );
            })}
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
              style={{
                left: textEditor.cssX,
                top: textEditor.cssY,
                fontFamily: fontFamilyRef.current,
                color: strokeColor,
                // A white-on-white input would be unreadable while typing —
                // give it a dark backing instead whenever the active color
                // is too light to read against the usual white/95 one.
                backgroundColor: strokeColor === "#ffffff" ? "rgba(30, 41, 59, 0.9)" : undefined,
              }}
              className={cn(
                "absolute z-10 min-w-[140px] -translate-y-1 rounded border border-[var(--accent)] px-1.5 py-0.5 text-base shadow outline-none",
                strokeColor === "#ffffff" ? "" : "bg-white/95"
              )}
            />
          )}
        </div>
        </div>
      </div>
    );
  }
);
