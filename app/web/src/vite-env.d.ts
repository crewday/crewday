/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_CREWDAY_PUBLIC_SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
