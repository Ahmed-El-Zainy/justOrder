/** Mirrors the SSE contract in specs/.../contracts/chat-api.md */

export type Phase =
  | 'understanding'
  | 'grounding'
  | 'generating'
  | 'validating'
  | 'executing'
  | 'synthesizing';

export interface Derivation {
  pipeline: unknown[];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number;
  attempts: number;
}

export interface ChartSpec {
  type: 'bar' | 'line';
  x_field: string;
  y_field: string;
  title: string;
}

export interface ClarificationCandidate {
  field: string;
  values: string[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  /** Streaming state, so the UI can show progress rather than an unexplained wait. */
  pending: boolean;
  phase?: Phase;
  rows?: Record<string, unknown>[];
  derivation?: Derivation;
  chart?: ChartSpec;
  clarification?: ClarificationCandidate[];
  error?: string;
}

export interface Suggestion {
  text: string;
  category: string;
}

export const PHASE_LABEL: Record<Phase, string> = {
  understanding: 'Reading the question',
  grounding: 'Matching names to the data',
  generating: 'Writing the query',
  validating: 'Checking the query is safe',
  executing: 'Running the query',
  synthesizing: 'Writing the answer',
};
