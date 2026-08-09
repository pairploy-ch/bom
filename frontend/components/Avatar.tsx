import { User as UserIcon } from "lucide-react";
import { cn } from "@/components/ui/primitives";

// Shared circular avatar — used by the top-right ProfileMenu (small) and the
// dedicated /profile settings page (large). Falls back to a generic user
// icon when no avatar has been uploaded yet.
export function Avatar({ src, size }: { src: string | null; size: number }) {
  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-[var(--accent)]/10 text-[var(--accent)]"
      )}
      style={{ width: size, height: size }}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt="" className="h-full w-full object-cover" />
      ) : (
        <UserIcon size={size * 0.5} />
      )}
    </div>
  );
}
