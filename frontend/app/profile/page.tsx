"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, User as UserIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useSupabaseUser } from "@/hooks/useSupabaseUser";
import { createClient } from "@/lib/supabase/client";
import { Avatar } from "@/components/Avatar";
import { Button, Card, CardHeader, Field, Input, Spinner } from "@/components/ui/primitives";
import { useToast } from "@/components/ui/Toast";

export default function ProfilePage() {
  const { user } = useSupabaseUser();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const avatarInput = useRef<HTMLInputElement>(null);

  const displayName = (user?.user_metadata?.display_name as string | undefined) || user?.email || "";

  useEffect(() => {
    setName(displayName);
  }, [displayName]);

  const hasAvatar = useQuery({
    queryKey: ["user-avatar-check", user?.id],
    queryFn: async () => {
      const res = await fetch(api.userAvatarUrl(user!.id));
      return res.ok;
    },
    enabled: !!user,
  });

  const saveName = useMutation({
    mutationFn: async (value: string) => {
      const supabase = createClient();
      const { error } = await supabase.auth.updateUser({ data: { display_name: value } });
      if (error) throw error;
    },
    onSuccess: () => toast.success("บันทึกชื่อที่แสดงแล้ว"),
    onError: (err) => toast.error(err instanceof Error ? err.message : "Failed to save name."),
  });

  const uploadAvatar = useMutation({
    mutationFn: (file: File) => api.uploadUserAvatar(user!.id, file),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-avatar-check", user?.id] });
      toast.success("อัปเดตรูปโปรไฟล์แล้ว");
    },
    onError: (err) => toast.error(err instanceof ApiError ? err.message : "Failed to upload avatar."),
  });

  if (!user) {
    return (
      <div className="w-full px-6 py-12">
        <Spinner />
      </div>
    );
  }

  const avatarSrc = hasAvatar.data ? api.userAvatarUrl(user.id) : null;

  return (
    <div className="w-full px-6 py-12">
      <header className="mb-8 flex items-center gap-3">
        <UserIcon className="text-slate-700" size={28} />
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">ตั้งค่าโปรไฟล์</h1>
          <p className="mt-1 text-sm text-slate-500">แก้ไขชื่อที่แสดงและรูปโปรไฟล์ของคุณ</p>
        </div>
      </header>

      <Card className="max-w-lg">
        <CardHeader title="โปรไฟล์" />
        <div className="space-y-6 p-6">
          <div className="flex flex-col items-center gap-3">
            <div className="relative">
              <Avatar src={avatarSrc} size={128} />
              <button
                type="button"
                onClick={() => avatarInput.current?.click()}
                disabled={uploadAvatar.isPending}
                aria-label="เปลี่ยนรูปโปรไฟล์"
                className="absolute bottom-0 right-0 flex h-9 w-9 items-center justify-center rounded-full border-2 border-white bg-[var(--accent)] text-white shadow-md transition-opacity hover:opacity-90 disabled:opacity-60"
              >
                {uploadAvatar.isPending ? <Spinner className="text-white" /> : <Camera size={16} />}
              </button>
              <input
                ref={avatarInput}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) uploadAvatar.mutate(file);
                  e.target.value = "";
                }}
              />
            </div>
            <p className="text-sm text-slate-500">{user.email}</p>
          </div>

          <div className="flex items-end gap-2">
            <div className="flex-1">
              <Field label="ชื่อที่แสดง">
                <Input value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
            </div>
            <Button
              onClick={() => saveName.mutate(name)}
              disabled={saveName.isPending || !name.trim() || name === displayName}
            >
              {saveName.isPending ? <Spinner /> : "บันทึก"}
            </Button>
          </div>
        </div>
      </Card>
    </div>
  );
}
