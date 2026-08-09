"use client";

import { LogIn, Sofa } from "lucide-react";
import { useState } from "react";
import { createClient } from "@/lib/supabase/client";
import { Alert, Button, Card, Field, Input, Spinner } from "@/components/ui/primitives";

// Invite-only — accounts are created by an admin in the Supabase dashboard
// (Authentication -> Users -> Add user), so there's deliberately no sign-up
// form here, just email/password sign-in.
export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    const supabase = createClient();
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    setLoading(false);
    if (error) {
      setError(error.message);
      return;
    }
    // Full reload (not router.push) so proxy.ts re-evaluates the session
    // cookie and every already-mounted query/state starts clean.
    const next = new URLSearchParams(window.location.search).get("next");
    window.location.href = next || "/";
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <Card className="w-full max-w-sm">
        <div className="space-y-5 p-6">
          <div className="flex flex-col items-center gap-2 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-[var(--accent)]/10 text-[var(--accent)]">
              <Sofa size={24} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-slate-900">SSK The Cat Workspace</h1>
              <p className="text-sm text-slate-500">เข้าสู่ระบบเพื่อใช้งาน</p>
            </div>
          </div>

          {error && <Alert tone="danger">{error}</Alert>}

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
            <Field label="รหัสผ่าน">
              <Input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </Field>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? <Spinner /> : <LogIn size={16} />}
              เข้าสู่ระบบ
            </Button>
          </form>
        </div>
      </Card>
    </div>
  );
}
