/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DEV_AUTH?: string;
  readonly VITE_DEV_WORKSPACE?: string;
  readonly VITE_DEV_USER?: string;
}
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
