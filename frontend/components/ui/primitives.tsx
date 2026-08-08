import {
  Alert as HeroAlert,
  Button as HeroButton,
  Card as HeroCard,
  Chip as HeroChip,
  Input as HeroInput,
  Spinner as HeroSpinner,
  TextArea as HeroTextArea,
} from "@heroui/react";
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  MouseEvent,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";

export function cn(...classes: (string | false | null | undefined)[]) {
  return classes.filter(Boolean).join(" ");
}

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";

// HeroUI's Button (React Aria Components under the hood) has its own event
// types (FocusEvent<Element> etc. rather than React's native
// FocusEvent<HTMLButtonElement>), so blanket-spreading native
// ButtonHTMLAttributes onto it doesn't type-check — only the props this
// app's call sites actually use are forwarded explicitly instead. It also
// omits `onClick` from its prop type entirely (uses `onPress`) and wants
// `isDisabled`, not the native `disabled` attribute, to drive its internal
// pressed/focus/aria-disabled state. Every call site here calls onClick
// with no args it reads, so the no-arg adapter below is safe.
export function Button({
  variant = "primary",
  type,
  className,
  disabled,
  onClick,
  children,
}: Pick<ButtonHTMLAttributes<HTMLButtonElement>, "type" | "className" | "disabled" | "onClick" | "children"> & {
  variant?: ButtonVariant;
}) {
  return (
    <HeroButton
      type={type}
      variant={variant}
      isDisabled={disabled}
      className={cn(className)}
      onPress={onClick ? () => onClick({} as MouseEvent<HTMLButtonElement>) : undefined}
    >
      {children}
    </HeroButton>
  );
}

export function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <HeroCard variant="default" className={cn("w-full", className)}>
      {children}
    </HeroCard>
  );
}

// HeroUI's own Card.Header is a plain stacking container, not this app's
// fixed title/description/right flex-row — so this stays hand-composed,
// just using HeroUI's Title/Description typography inside it.
export function CardHeader({
  title,
  description,
  right,
  icon,
}: {
  title: string;
  description?: string;
  right?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-4">
      <div className="flex items-start gap-2.5">
        {icon && <span className="mt-0.5 shrink-0 text-slate-400">{icon}</span>}
        <div>
          <HeroCard.Title className="text-base font-semibold text-slate-900">{title}</HeroCard.Title>
          {description && <HeroCard.Description className="mt-0.5 text-sm text-slate-500">{description}</HeroCard.Description>}
        </div>
      </div>
      {right}
    </div>
  );
}

// Unlike Button, HeroUI's Input wraps a real native <input> and forwards
// standard DOM attributes/events directly — type="number", the existing
// onChange={(e) => e.target.value} pattern, and disabled all just work.
export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <HeroInput fullWidth {...props} className={cn(props.className)} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <HeroTextArea fullWidth {...props} className={cn(props.className)} />;
}

// Kept as a native <select>, just restyled — HeroUI's real Select is a
// compound React-Aria listbox/popover that doesn't render <option> children
// and hands back a raw Key from onChange (not a ChangeEvent), incompatible
// with this app's ~15+ call sites (order-type/room/sheet-name dropdowns,
// including a "+ new room..." sentinel <option>) without rewriting every one.
export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={cn(
        "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition-shadow focus:border-[var(--accent)] focus:ring-2 focus:ring-[var(--accent)]/20",
        props.className
      )}
    />
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-400">{hint}</span>}
    </label>
  );
}

type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

// Maps to HeroUI's Chip, not HeroUI's own "Badge" — that's an overlay
// notification-dot meant to anchor onto another element (e.g. on an
// Avatar), not a standalone pill; Chip is HeroUI's actual label component.
const BADGE_TONE_MAP: Record<BadgeTone, "default" | "success" | "warning" | "danger" | "accent"> = {
  neutral: "default",
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "accent",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: React.ReactNode }) {
  return (
    <HeroChip color={BADGE_TONE_MAP[tone]} variant="soft" size="sm">
      {children}
    </HeroChip>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <HeroSpinner size="sm" color="current" className={cn(className)} aria-label="Loading" />;
}

export function EmptyState({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-6 py-10 text-center text-sm text-slate-500">
      {children}
    </div>
  );
}

type AlertTone = "info" | "warning" | "danger" | "success";

// HeroUI's status enum has no "info" value — map it to "accent", the
// closest neutral-but-highlighted semantic.
const ALERT_TONE_MAP: Record<AlertTone, "accent" | "warning" | "danger" | "success"> = {
  info: "accent",
  warning: "warning",
  danger: "danger",
  success: "success",
};

export function Alert({ tone = "info", children }: { tone?: AlertTone; children: React.ReactNode }) {
  return (
    <HeroAlert status={ALERT_TONE_MAP[tone]}>
      <HeroAlert.Indicator />
      <HeroAlert.Content>{children}</HeroAlert.Content>
    </HeroAlert>
  );
}
