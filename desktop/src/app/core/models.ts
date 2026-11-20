// Shapes returned by the local API (see app/schemas/chat.py and app/conversations/schemas.py).

export interface ChatReply {
  model: string;
  answer: string;
  conversation_id?: string;
  agent_id?: string;
  agent_name?: string;
  memory_intent?: string;
  memory_action_id?: string;
  confirmation_required?: boolean;
  selected_agent_name?: string;
}

export interface Conversation {
  id: string;
  title: string;
  agent_id: string | null;
  created_at: string;
  updated_at: string;
  is_archived: boolean;
  message_count: number;
}

export interface ConversationList {
  conversations: Conversation[];
  count: number;
  limit: number;
  offset: number;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: string;
  content: string;
  agent_id: string | null;
  model: string | null;
  created_at: string;
  sequence_number: number;
}

export interface MessageList {
  messages: Message[];
  count: number;
  limit: number;
  offset: number;
}

// Dashboard data (see app/analytics.py).

export type AnalyticsRange = 'today' | '7d' | '30d';

export interface CountWithPrevious {
  value: number;
  previous: number;
}

export interface AgentActivity {
  id: string;
  name: string;
  role: string;
  enabled: boolean;
  members: string[];
  chat_turns: number;
  tool_actions: number;
  lessons_pending: number;
  skills_active: number;
  study_runs: number;
  last_active_at: string | null;
}

export interface QueueItem {
  id: string;
  tool: string;
  agent_id: string | null;
  agent_name: string | null;
  description: string;
  risk: string;
  status: string;
  created_at: string | null;
}

export interface AnalyticsSummary {
  range: AnalyticsRange;
  since: string;
  generated_at: string;
  paused: boolean;
  kpis: {
    chat_turns: CountWithPrevious;
    conversations: CountWithPrevious;
    tool_actions: { value: number; completed: number; failed: number; success_rate: number | null };
    approvals_pending: { value: number; oldest_at: string | null };
    lessons_new: { value: number; pending: number };
    budget: { tool_calls_used: number; tool_calls_limit: number; chat_turns_used: number; chat_turns_limit: number };
  };
  agents: AgentActivity[];
  systems: {
    memories: { count: number; added: number };
    knowledge: { documents: number; chunks: number };
    projects: { count: number; entries: number };
    graph: { entities: number; relations: number };
    tools: { denied: number };
  };
  queue: QueueItem[];
  trend: { step: 'hour' | 'day'; starts: string[]; chat_turns: number[]; tool_completed: number[]; tool_failed: number[] };
  tools: { tool: string; actions: number; failed: number; denied: number }[];
  learning: {
    lessons_active: number;
    lessons_pending: number;
    skills_active: number;
    skills_candidate: number;
    study_runs: number;
    memories_added: number;
  };
}

// Approvals (see app/approvals/service.py to_public and review.py).

export interface ApprovalReview {
  reviewable: boolean;
  verified: boolean;
  mcp_execution_available: boolean;
  arguments_text: string | null;
  hidden: string[] | number | null;
  warnings: string[];
  mcp: { applies: boolean };
}

export interface ApprovalChange {
  operation: 'create' | 'update' | 'delete';
  relative_path: string;
  size_bytes: number;
  diff: string;
}

export interface Approval {
  id: string;
  agent_id: string | null;
  tool_name: string;
  action: string;
  resource_type: string;
  resource_id: string;
  description: string;
  change: ApprovalChange | null;
  risk: string;
  reason: string;
  mode: string;
  status: string;
  created_at: string;
  expires_at: string | null;
  decided_at: string | null;
  execution_status: string | null;
  result: string | null;
  outcome?: string;
  output?: unknown;
  review: ApprovalReview;
}

export interface ApprovalList {
  approvals: Approval[];
  count: number;
}

export interface AgentSummary {
  id: string;
  name: string;
  role: string;
  enabled: boolean;
}
