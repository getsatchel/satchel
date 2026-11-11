import { api, keyPath } from "./client";
import type {
  AssetDetail,
  AssetPage,
  ChangeDecision,
  ChangesOut,
  ConnectionCreate,
  ConnectionKind,
  GitTestOut,
  GitTestRequest,
  DeploymentInfo,
  LimitRow,
  LimitValue,
  ConnectionOut,
  ConnectionPatch,
  ConnectionSaved,
  ConnectionTestOut,
  ConnectionTestRequest,
  CrawlOut,
  EvalCompare,
  EvalGate,
  EvalRun,
  EvalRunDetail,
  EvalSuite,
  EvalSuiteDetail,
  CreateItemRequest,
  CurateRunOut,
  DiffOut,
  ItemDetail,
  ItemPage,
  LlmConnection,
  LlmConnectionCreate,
  LlmConnectionPatch,
  LlmConnections,
  LlmTestOut,
  LlmTestRequest,
  Me,
  ObservabilitySummary,
  PackOut,
  PreviewOut,
  ProposalOut,
  ProposalResult,
  ReleaseOut,
  ReleasePage,
  SearchHit,
  SessionDetail,
  SessionPage,
  SessionRow,
  SourceCreate,
  SourceKindOut,
  SourceOut,
  SourceRemovedOut,
  WebhookRemovedOut,
  WebhookSecretOut,
  SyncReportOut,
  SuggestionDetail,
  SuggestionList,
  SuggestionSummary,
  VersionOut,
} from "./types";

export interface ItemFilters {
  type?: string;
  state?: string;
  scope?: string;
  managed_by?: string;
  q?: string;
  limit?: number;
  offset?: number;
}

export const endpoints = {
  me: () => api.get<Me>("/v1/me"),

  listItems: (f: ItemFilters) => api.get<ItemPage>("/v1/items", { limit: 200, ...f }),
  getItem: (key: string) => api.get<ItemDetail>(`/v1/items/${keyPath(key)}`),
  getVersion: (key: string, seq: number) =>
    api.get<VersionOut>(`/v1/items/${keyPath(key)}/versions/${seq}`),
  diffItem: (key: string, from?: number, to?: number) =>
    api.get<DiffOut>(`/v1/items/${keyPath(key)}/diff`, { from, to }),
  createItem: (req: CreateItemRequest) => api.post<ItemDetail>("/v1/items", req),
  updateItem: (key: string, body: string, meta: Record<string, unknown>) =>
    api.put<ItemDetail>(`/v1/items/${keyPath(key)}`, { body, meta }),
  deprecateItem: (key: string) => api.post<ItemDetail>(`/v1/items/${keyPath(key)}/deprecate`),
  updateOverlay: (key: string, overlay: Record<string, unknown>) =>
    api.put<ItemDetail>(`/v1/items/${keyPath(key)}/overlay`, { overlay }),

  listLlmConnections: () => api.get<LlmConnections>("/v1/llm-connections"),
  createLlmConnection: (req: LlmConnectionCreate) => api.post<LlmConnection>("/v1/llm-connections", req),
  updateLlmConnection: (id: string, req: LlmConnectionPatch) => api.patch<LlmConnection>(`/v1/llm-connections/${id}`, req),
  deleteLlmConnection: (id: string) => api.del(`/v1/llm-connections/${id}`),
  testLlmConnection: (req: LlmTestRequest) => api.post<LlmTestOut>("/v1/llm-connections/test", req),

  listSources: () => api.get<SourceOut[]>("/v1/sources"),
  listSourceKinds: () => api.get<SourceKindOut[]>("/v1/source-kinds"),
  syncSource: (id: string) => api.post<SyncReportOut>(`/v1/sources/${id}/sync`),
  syncSourceWithFiles: (id: string, form: FormData) => api.upload<SyncReportOut>(`/v1/sources/${id}/sync`, form),
  createSource: (req: SourceCreate) => api.post<SourceOut>("/v1/sources", req),
  updateSource: (id: string, config: Record<string, unknown>) => api.put<SourceOut>(`/v1/sources/${id}`, { config }),
  deleteSource: (id: string) => api.del<SourceRemovedOut>(`/v1/sources/${id}`),
  setSourceWebhook: (id: string) => api.post<WebhookSecretOut>(`/v1/sources/${id}/webhook`),
  removeSourceWebhook: (id: string) => api.del<WebhookRemovedOut>(`/v1/sources/${id}/webhook`),

  testGit: (req: GitTestRequest) => api.post<GitTestOut>("/v1/sources/git/test", req),
  listConnectionKinds: () => api.get<ConnectionKind[]>("/v1/connection-kinds"),
  getLimits: () => api.get<LimitRow[]>("/v1/settings/limits"),
  putLimits: (values: Record<string, LimitValue>) => api.put<LimitRow[]>("/v1/settings/limits", { values }),
  getDeployment: () => api.get<DeploymentInfo>("/v1/settings/deployment"),
  createConnection: (req: ConnectionCreate) => api.post<ConnectionSaved>("/v1/connections", req),
  updateConnection: (id: string, req: ConnectionPatch) => api.patch<ConnectionSaved>(`/v1/connections/${id}`, req),
  deleteConnection: (id: string) => api.del(`/v1/connections/${id}`),
  testConnection: (req: ConnectionTestRequest) => api.post<ConnectionTestOut>("/v1/connections/test", req),
  crawlConnection: (id: string) => api.post<CrawlOut>(`/v1/connections/${id}/crawl`),

  listProposals: () => api.get<{ proposals: ProposalOut[] }>("/v1/proposals"),
  acceptProposal: (key: string, req: { key?: string; body?: string; meta?: Record<string, unknown> }) =>
    api.post<ProposalResult>(`/v1/proposals/${keyPath(key)}/accept`, req),
  rejectProposal: (key: string, reason: string) =>
    api.post<ProposalResult>(`/v1/proposals/${keyPath(key)}/reject`, { reason }),

  listSuggestions: (q: { status?: string; sort?: string; source?: string; limit?: number }) =>
    api.get<SuggestionList>("/v1/suggestions", { limit: 100, ...q }),
  getSuggestion: (id: string) => api.get<SuggestionDetail>(`/v1/suggestions/${id}`),
  acceptSuggestionChange: (
    id: string,
    changeId: string,
    req: { body?: string; meta?: Record<string, unknown>; key?: string },
  ) => api.post<ChangeDecision>(`/v1/suggestions/${id}/changes/${changeId}/accept`, req),
  rejectSuggestionChange: (id: string, changeId: string, reason: string | null) =>
    api.post<ChangeDecision>(`/v1/suggestions/${id}/changes/${changeId}/reject`, { reason }),
  dismissSuggestion: (id: string, reason: string) =>
    api.post<SuggestionSummary>(`/v1/suggestions/${id}/dismiss`, { reason }),
  completeSuggestion: (id: string) => api.post<SuggestionSummary>(`/v1/suggestions/${id}/complete`),
  runCuration: (useLlm: boolean) => api.post<CurateRunOut>("/v1/curate/run", { use_llm: useLlm }),

  listChanges: () => api.get<ChangesOut>("/v1/changes"),
  createPreview: (keys: string[] | null, note: string | null) =>
    api.post<PreviewOut>("/v1/previews", { keys, note }),
  getPreview: (id: string) => api.get<PreviewOut>(`/v1/previews/${id}`),
  publish: (id: string, overrideReason?: string) =>
    api.post<ReleaseOut>(`/v1/previews/${id}/publish`, overrideReason ? { override_reason: overrideReason } : {}),
  listReleases: (kind: "published" | "preview" = "published") =>
    api.get<ReleasePage>("/v1/releases", { kind, limit: 100 }),
  rollback: (id: string) => api.post<ReleaseOut>(`/v1/releases/${id}/rollback`),

  search: (query: string, k = 8) => api.post<SearchHit[]>("/v1/search", { query, k }),
  pack: (question: string) => api.post<PackOut>("/v1/context-pack", { question }),
  previewSearch: (id: string, query: string, k = 8) =>
    api.post<SearchHit[]>(`/v1/previews/${id}/search`, { query, k }),
  previewPack: (id: string, question: string) =>
    api.post<PackOut>(`/v1/previews/${id}/context-pack`, { question }),

  summary: (days: number) => api.get<ObservabilitySummary>("/v1/observability/summary", { days }),
  listSessions: (q: { client?: string; status?: string; limit?: number; offset?: number }) =>
    api.get<SessionPage>("/v1/sessions", { limit: 50, ...q }),
  getSession: (id: string) => api.get<SessionDetail>(`/v1/sessions/${id}`),
  setSensitive: (id: string, sensitive: boolean) =>
    api.post<SessionRow>(`/v1/sessions/${id}/sensitive`, { sensitive }),

  listConnections: () => api.get<ConnectionOut[]>("/v1/connections"),
  listAssets: (q: { connection?: string; q?: string }) =>
    api.get<AssetPage>("/v1/assets", { limit: 500, ...q }),
  getAsset: (id: string) => api.get<AssetDetail>(`/v1/assets/${id}`),

  listEvalSuites: () => api.get<EvalSuite[]>("/v1/evals/suites"),
  getEvalSuite: (key: string) => api.get<EvalSuiteDetail>(`/v1/evals/suites/${keyPath(key)}`),
  saveEvalSuite: (yaml: string) => api.post<EvalSuite>("/v1/evals/suites", { yaml }),
  listEvalRuns: (q: { suite_key?: string; release_id?: string; limit?: number }) =>
    api.get<EvalRun[]>("/v1/evals/runs", { limit: 100, ...q }),
  getEvalRun: (id: string) => api.get<EvalRunDetail>(`/v1/evals/runs/${id}`),
  startEvalRun: (suiteKey: string, releaseId: string, model?: string) =>
    api.post<EvalRun>("/v1/evals/runs", { suite_key: suiteKey, release_id: releaseId, model: model || null }),
  compareEvalRuns: (a: string, b: string) => api.get<EvalCompare>("/v1/evals/compare", { a, b }),
  previewGate: (id: string) => api.get<EvalGate>(`/v1/previews/${id}/evals`),
  runPreviewEvals: (id: string) => api.post<EvalGate>(`/v1/previews/${id}/evals`, {}),
};
