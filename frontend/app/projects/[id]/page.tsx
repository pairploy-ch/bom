"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

// Matches the original app's default: it always opened on Tab 1 first.
export default function ProjectIndexPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  useEffect(() => {
    router.replace(`/projects/${id}/step-1`);
  }, [id, router]);
  return null;
}
