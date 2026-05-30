import { createContext, useCallback, use, useMemo, useState, type ReactNode } from "react";
import { persistRole, readRoleCookie } from "@/lib/preferences";
import type { Role } from "@/types/api";

interface RoleCtx {
  role: Role;
  setRole: (r: Role) => void;
}

const Ctx = createContext<RoleCtx | null>(null);

export function RoleProvider({ children }: { children: ReactNode }) {
  const [role, setRoleState] = useState<Role>(() => readRoleCookie());
  const setRole = useCallback((r: Role) => {
    setRoleState(r);
    persistRole(r);
  }, []);
  const value = useMemo(() => ({ role, setRole }), [role, setRole]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRole(): RoleCtx {
  const v = use(Ctx);
  if (!v) throw new Error("useRole must be used inside <RoleProvider>");
  return v;
}
