import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useProject(id: string) {
  return useQuery({
    queryKey: ["project", id],
    queryFn: () => api.getProject(id),
    enabled: !!id,
  });
}

export function projectKey(id: string) {
  return ["project", id] as const;
}
