import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function useHouse(id: string) {
  return useQuery({
    queryKey: ["house", id],
    queryFn: () => api.getHouse(id),
    enabled: !!id,
  });
}

export function houseKey(id: string) {
  return ["house", id] as const;
}
