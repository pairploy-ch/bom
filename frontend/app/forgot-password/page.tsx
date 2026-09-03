"use client";

import { ArrowLeft, MailCheck, SendHorizonal } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { Alert, Button, Card, Field, Input, Spinner } from "@/components/ui/primitives";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [sent, setSent] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const supabase = createClient();
    const { error } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: `${window.location.origin}/reset-password`,
    });
    setLoading(false);
    if (error) {
      setError(error.message);
      return;
    }
    setSent(true);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <Card className="w-full max-w-sm">
        <div className="space-y-5 p-6">
          <div className="flex flex-col items-center gap-2 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--accent)]/10 text-[var(--accent)]">
              <MailCheck size={24} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-slate-900">ลืมรหัสผ่าน</h1>
              <p className="text-sm text-slate-500">กรอกอีเมลเพื่อรับลิงก์ตั้งรหัสผ่านใหม่</p>
            </div>
          </div>

          {error && <Alert tone="danger">{error}</Alert>}

          {sent ? (
            <Alert tone="success">
              หากอีเมลนี้มีอยู่ในระบบ เราได้ส่งลิงก์สำหรับตั้งรหัสผ่านใหม่ไปให้แล้ว กรุณาตรวจสอบกล่องอีเมลของคุณ
            </Alert>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              <Field label="อีเมล">
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  autoComplete="email"
                  required
                />
              </Field>
              <Button type="submit" className="w-full" disabled={loading}>
                {loading ? <Spinner /> : <SendHorizonal size={16} />}
                ส่งลิงก์ตั้งรหัสผ่านใหม่
              </Button>
            </form>
          )}

          <Link
            href="/login"
            className="flex items-center justify-center gap-1.5 text-xs text-slate-500 hover:text-[var(--accent)]"
          >
            <ArrowLeft size={14} />
            กลับไปหน้าเข้าสู่ระบบ
          </Link>
        </div>
      </Card>
    </div>
  );
}
