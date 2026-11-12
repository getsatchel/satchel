import type { components } from "./schema";

type S = components["schemas"];

/** The server always sends fields that have defaults, but OpenAPI marks them optional. */
type Req<T> = { [K in keyof T]-?: T[K] };


export type Me = S["MeOut"];
export type ItemUsage = Req<S["ItemUsage"]>;
export type ItemSummary = Omit<Req<S["ItemSummary"]>, "usage"> & { usage: ItemUsage };
export type ItemPage = Omit<S["ItemPage"], "items"> & { items: ItemSummary[] };
export type ItemDetail = Omit<Req<S["ItemDetail"]>, "usage" | "versions" | "meta"> & {
  usage: ItemUsage;
  versions: VersionSummary[];
  meta: Record<string, unknown>;
};
export type VersionSummary = S["VersionSummary"];
export type VersionOut = S["VersionOut"];
export type DiffOut = S["DiffOut"];
export type CreateItemRequest = S["CreateItemRequest"];
export type ChangeOut = S["ChangeOut"];
export type ChangesOut = S["ChangesOut"];
export type PreviewOut = Req<S["PreviewOut"]>;
export type ReleaseOut = Req<S["ReleaseOut"]>;
export type ReleasePage = Omit<S["ReleasePage"], "releases"> & { releases: ReleaseOut[] };
export type ProposalOut = S["ProposalOut"];
export type ProposalResult = S["ProposalResult"];
export type SearchHit = S["SearchHitOut"];
export type PackOut = S["PackOut"];
export type PackEntry = S["PackEntryOut"];
export type ObservabilitySummary = S["ObservabilitySummary"];
export type SessionRow = S["SessionRowOut"];
export type SessionPage = S["SessionPage"];
export type SessionDetail = S["SessionDetail"];
export type ToolCall = S["ToolCallOut"];
export type ConnectionOut = S["ConnectionOut"];
export type AssetNode = Omit<Req<S["AssetNode"]>, "children"> & { children: AssetNode[] };
export type AssetPage = Omit<S["AssetPage"], "assets"> & { assets: AssetNode[] };
export type AssetDetail = AssetNode & Omit<S["AssetDetail"], keyof AssetNode>;

export type SuggestionSummary = S["SuggestionSummary"];
export type SuggestionList = S["SuggestionList"];
export type SuggestionDetail = S["SuggestionDetail"];
export type SuggestionChange = S["SuggestionChangeOut"];
export type ChangeDecision = S["ChangeDecision"];
export type EvidenceSession = S["EvidenceSession"];
export type CurateRunOut = S["CurateRunOut"];

// ---- evals (M5) ----
export type EvalSuite = Req<S["SuiteOut"]>;
export type EvalSuiteDetail = Req<S["SuiteDetail"]>;
export type MeasureStat = S["MeasureStat"];
export type EvalRun = Req<S["RunOut"]>;
export type EvalRunDetail = Omit<Req<S["RunDetail"]>, "cases"> & { cases: EvalCase[] };
export type EvalCase = S["CaseOut"];
export type EvalAttempt = S["AttemptOut"];
export type EvalGrade = Req<S["GradeOut"]>;
export type EvalCompare = S["CompareOut"];
export type MeasureDelta = S["MeasureDelta"];
export type CaseMove = S["CaseMove"];
export type EvalGate = S["GateOut"];
export type EvalGateSuite = Req<S["GateSuiteOut"]>;

export type SourceKindOut = Req<S["SourceKindOut"]>;
export type SourceOut = Omit<Req<S["SourceOut"]>, "adapter"> & { adapter: SourceKindOut | null };
export type SyncReportOut = S["SyncReportOut"];

export type LlmConnection = Req<S["LlmConnectionOut"]>;
export type LlmDeployment = S["DeploymentLlmOut"];
export type LlmConnections = Omit<Req<S["LlmConnectionsOut"]>, "connections"> & { connections: LlmConnection[] };
export type LlmConnectionCreate = S["LlmConnectionCreate"];
export type LlmConnectionPatch = S["LlmConnectionPatch"];
export type LlmTestRequest = Partial<S["LlmTestRequest"]>;
export type LlmTestOut = S["LlmTestOut"];

export type ConnectionKind = Req<S["ConnectionKindOut"]>;
export type ConnectionKindField = S["KindFieldOut"];
export type ConnectionSaved = S["ConnectionSavedOut"];
export type ConnectionCreate = S["ConnectionCreate"];
export type ConnectionPatch = S["ConnectionPatch"];
export type ConnectionTestRequest = Partial<S["ConnectionTestRequest"]>;
export type ConnectionTestOut = S["ConnectionTestOut"];
export type CrawlOut = S["CrawlOut"];
export type JobOut = S["JobOut"];
export type SourceCreate = S["SourceCreate"];
export type SourceRemovedOut = S["SourceRemovedOut"];
export type WebhookSecretOut = S["WebhookSecretOut"];
export type WebhookRemovedOut = S["WebhookRemovedOut"];
export type LimitRow = S["LimitOut"];
export type LimitValue = number | null | "default";
export type DeploymentInfo = S["DeploymentOut"];
export type DeploymentGroup = S["DeploymentGroup"];
export type GitTestRequest = S["GitTestRequest"];
export type GitTestOut = S["GitTestOut"];
