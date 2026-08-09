"use client";

import { useEffect } from "react";
import { useParams, useRouter } from "next/navigation";

// Matches the original app's default: it always opened on Tab 1 first.
export default function HouseIndexPage() {
  const { projectId, houseId } = useParams<{ projectId: string; houseId: string }>();
  const router = useRouter();
  useEffect(() => {
    router.replace(`/projects/${projectId}/houses/${houseId}/step-1`);
  }, [projectId, houseId, router]);
  return null;
}
