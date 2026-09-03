"use client";

import { KeyRound, ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { Alert, Button, Card, Field, Input, Spinner } from "@/components/ui/primitives";

type LinkState = "checking" | "ready" | "invalid";

export default function ResetPasswordPage() {
  const router = useRouter();
  const [linkState, setLinkState] = useState<LinkState>("checking");
  const [linkError, setLinkError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  // The recovery email link lands here as /reset-password?code=... (PKCE) —
  // exchange it for a session before letting the user set a new password.
  useEffect(() => {
    async function checkLink() {
      const params = new URLSearchParams(window.location.search);
      const code = params.get("code");
      const errorDescription = params.get("error_description");
      if (errorDescription) {
        setLinkError(errorDescription);
        setLinkState("invalid");
        return;
      }
      if (!code) {
        setLinkState("invalid");
        return;
      }
      const supabase = createClient();
      const { error } = await supabase.auth.exchangeCodeForSession(code);
      if (error) {
        setLinkError(error.message);
        setLinkState("invalid");
        return;
      }
      setLinkState("ready");
    }
    checkLink();
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password.length < 6) {
      setError("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร");
      return;
    }
    if (password !== confirmPassword) {
      setError("รหัสผ่านทั้งสองช่องไม่ตรงกัน");
      return;
    }
    setLoading(true);
    const supabase = createClient();
    const { error } = await supabase.auth.updateUser({ password });
    setLoading(false);
    if (error) {
      setError(error.message);
      return;
    }
    setDone(true);
    // Full reload (not router.push) so proxy.ts re-evaluates the now-signed-in
    // session cookie — same reasoning as the login page's redirect.
    window.setTimeout(() => {
      window.location.assign("/");
    }, 1500);
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <Card className="w-full max-w-sm">
        <div className="space-y-5 p-6">
          <div className="flex flex-col items-center gap-2 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--accent)]/10 text-[var(--accent)]">
              <KeyRound size={24} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-slate-900">ตั้งรหัสผ่านใหม่</h1>
              <p className="text-sm text-slate-500">กรอกรหัสผ่านใหม่ที่ต้องการใช้เข้าสู่ระบบ</p>
            </div>
          </div>

          {linkState === "checking" && (
            <div className="flex justify-center py-4">
              <Spinner />
            </div>
          )}

          {linkState === "invalid" && (
            <>
              <Alert tone="danger">{linkError || "ลิงก์ไม่ถูกต้องหรือหมดอายุแล้ว กรุณาขอลิงก์ใหม่อีกครั้ง"}</Alert>
              <Button className="w-full" onClick={() => router.push("/forgot-password")}>
                ขอลิงก์ตั้งรหัสผ่านใหม่
              </Button>
            </>
          )}

          {linkState === "ready" && !done && (
            <>
              {error && <Alert tone="danger">{error}</Alert>}
              <form onSubmit={handleSubmit} className="space-y-4">
                <Field label="รหัสผ่านใหม่">
                  <Input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoComplete="new-password"
                    required
                  />
                </Field>
                <Field label="ยืนยันรหัสผ่านใหม่">
                  <Input
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    autoComplete="new-password"
                    required
                  />
                </Field>
                <Button type="submit" className="w-full" disabled={loading}>
                  {loading ? <Spinner /> : <KeyRound size={16} />}
                  บันทึกรหัสผ่านใหม่
                </Button>
              </form>
            </>
          )}

          {done && <Alert tone="success">ตั้งรหัสผ่านใหม่เรียบร้อยแล้ว กำลังพาเข้าสู่ระบบ...</Alert>}

          {linkState !== "checking" && !done && (
            <Link
              href="/login"
              className="flex items-center justify-center gap-1.5 text-xs text-slate-500 hover:text-[var(--accent)]"
            >
              <ArrowLeft size={14} />
              กลับไปหน้าเข้าสู่ระบบ
            </Link>
          )}
        </div>
      </Card>
    </div>
  );
}
