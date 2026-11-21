export type EntityLabel =
  | "Herb"
  | "Formula"
  | "Disease"
  | "Symptom"
  | "Effect"
  | "Source";

export type MatchType = "name" | "alias" | "fuzzy" | "semantic";

export interface LinkedEntity {
  label: EntityLabel;
  name: string;
  matched_text: string;
  match_type: MatchType;
  score: number;
}

export interface Evidence {
  subject: string;
  subject_label: EntityLabel;
  relationship: string;
  object: string;
  object_label: EntityLabel;
  source: string | null;
}

export interface AttributeFact {
  subject: string;
  subject_label: EntityLabel;
  attribute: string;
  attribute_label: string;
  value: string;
  source: string | null;
}

export interface AggregateFact {
  anchor: string;
  anchor_label: EntityLabel;
  relationship: string;
  result_label: EntityLabel;
  direction: "outgoing" | "incoming";
  count: number;
  description: string;
}

export interface PathStepEvidence {
  subject: string;
  subject_label: EntityLabel;
  relationship: string;
  object: string;
  object_label: EntityLabel;
}

export interface PathEvidence {
  root: string;
  root_label: EntityLabel;
  target: string;
  target_label: EntityLabel;
  source: string | null;
  steps: PathStepEvidence[];
}

export interface AnswerVerification {
  status: "passed" | "fallback";
  agent: "evidence_audit";
  checked_items: number;
  matched_items: number;
  evidence_items: number;
  fact_items: number;
  aggregate_items: number;
  path_items: number;
  missing_sources: number;
  fallback_used: boolean;
  message: string;
}

export interface QueryInfo {
  cypher: string;
  parameters: Record<string, unknown>;
  plan_kind: string | null;
}

export interface GraphQAError {
  code: string;
  message: string;
}

export interface GraphQAResponse {
  schema_version: "1.0";
  request_id: string;
  question: string;
  answer: string;
  grounded: boolean;
  linked_entities: LinkedEntity[];
  query: QueryInfo | null;
  evidence: Evidence[];
  facts: AttributeFact[];
  aggregates: AggregateFact[];
  paths: PathEvidence[];
  verification: AnswerVerification | null;
  warnings: string[];
  error: GraphQAError | null;
  session_id?: string | null;
  rewritten_question?: string | null;
}

export interface ApiWarning {
  code: string;
  message: string;
}

export interface ApiError {
  code: string;
  message: string;
}

export interface ResponseMeta {
  schema_version: "1.0";
  tool_version: string | null;
  skill_version: string | null;
  duration_ms: number;
  degraded: boolean;
}

export interface ApiEnvelope<T> {
  request_id: string;
  run_id: string | null;
  data: T | null;
  evidence: Evidence[];
  warnings: ApiWarning[];
  error: ApiError | null;
  meta: ResponseMeta;
}

export interface StagePayload {
  stage: string;
  label: string;
  agent?: string;
  plan_kind?: string | null;
  plan_label?: string | null;
  step?: number;
  decision?: string;
  reason_code?: string;
}

export type QAStreamEventName =
  | "stage"
  | "evidence"
  | "facts"
  | "aggregates"
  | "paths"
  | "audit"
  | "answer_delta"
  | "warning"
  | "error"
  | "done";

export interface QAStreamEvent {
  event: QAStreamEventName | string;
  data: Record<string, unknown>;
}
