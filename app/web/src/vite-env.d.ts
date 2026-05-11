/// <reference types="vite/client" />

declare module "lucide-react/dist/esm/icons/*.mjs" {
  import type { LucideIcon } from "lucide-react";

  const Icon: LucideIcon;
  export default Icon;
}

interface ImportMetaEnv {
  readonly VITE_CREWDAY_PUBLIC_SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
