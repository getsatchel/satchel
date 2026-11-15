import { useQuery } from "@tanstack/react-query";
import { endpoints } from "@/api/endpoints";

/** Workspace roles, lowest first (mirrors ROLE_RANK in satchel/mcp/auth.py). */
export const ROLES = ["explorer", "analyst", "curator", "admin"] as const;
export type Role = (typeof ROLES)[number];

/** Whether `role` is `min` or higher. An unknown or missing role counts as the lowest. */
export function atLeast(role: string | null | undefined, min: Role): boolean {
  return ROLES.indexOf((role ?? "explorer") as Role) >= ROLES.indexOf(min);
}

/** The signed-in member and a role check. The server enforces the same rules; this only decides what to show. */
export function useRole() {
  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me, staleTime: 60_000, retry: false });
  const role = me.data?.role;
  return { me, role, loaded: me.data !== undefined, can: (min: Role) => atLeast(role, min) };
}
