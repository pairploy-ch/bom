"use client";

import { useQuery } from "@tanstack/react-query";
import { Settings } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useSupabaseUser } from "@/hooks/useSupabaseUser";
import { Avatar } from "@/components/Avatar";

// Top-right profile pill: avatar + display name, opens a small dropdown
// with a link out to the dedicated /profile settings page — actual editing
// (rename, change picture at full size) lives there, not inline here.
export function ProfileMenu() {
  const { user } = useSupabaseUser();
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const displayName = (user?.user_metadata?.display_name as string | undefined) || user?.email || "";

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, [open]);

  const hasAvatar = useQuery({
    queryKey: ["user-avatar-check", user?.id],
    queryFn: async () => {
      const res = await fetch(api.userAvatarUrl(user!.id));
      return res.ok;
    },
    enabled: !!user,
  });

  if (!user) return null;

  const avatarSrc = hasAvatar.data ? api.userAvatarUrl(user.id) : null;

  return (
    <div ref={wrapperRef} className="relative ml-auto">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-full py-1 pl-1 pr-3 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-100"
      >
        <Avatar src={avatarSrc} size={28} />
        <span className="max-w-[10rem] truncate">{displayName}</span>
      </button>

      {open && (
        <div className="absolute right-0 top-full z-20 mt-2 w-64 rounded-xl border border-slate-200 bg-white p-2 shadow-lg">
          <div className="flex items-center gap-3 px-2 py-2">
            <Avatar src={avatarSrc} size={40} />
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-slate-900">{displayName}</p>
              <p className="truncate text-xs text-slate-400">{user.email}</p>
            </div>
          </div>
          <Link
            href="/profile"
            onClick={() => setOpen(false)}
            className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900"
          >
            <Settings size={16} />
            ตั้งค่าโปรไฟล์
          </Link>
        </div>
      )}
    </div>
  );
}
