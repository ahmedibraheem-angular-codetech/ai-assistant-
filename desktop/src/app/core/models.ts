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
    tool_actions: {
      value: number;
      completed: number;
      failed: number;
      refused?: number;
      success_rate: number | null;
      avg_duration_ms?: number | null;
    };
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
  tools: { tool: string; actions: number; failed: number; denied: number; avg_duration_ms?: number | null }[];
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

// Team, profiles and hiring (app/api/routes/agents.py, learning.py, control.py).

export interface Agent extends AgentSummary {
  description: string;
  system_prompt: string;
  model: string;
  created_at: string;
  updated_at: string;
}

export interface AgentTemplate {
  id: string;
  name: string;
  title_ar: string;
  role: string;
  description: string;
  tools: string[];
  daily_tool_calls: number | null;
  daily_chat_turns: number | null;
  notes: string[];
}

export interface HireResult {
  agent: Agent;
  template_id: string;
  granted_tools: string[];
  budget: unknown;
  next_steps: string[];
}

export interface Lesson {
  id: string;
  agent_id: string;
  text: string;
  topic: string | null;
  source: string;
  status: 'active' | 'pending' | 'retired';
  uses: number;
  created_at: string;
}

export interface Skill {
  id: string;
  agent_id: string;
  name: string;
  version: number;
  description: string;
  steps: string;
  status: 'candidate' | 'pending' | 'active' | 'rejected' | 'retired';
  source: string;
  created_at: string;
}

export interface EvalRun {
  id: string;
  skill_id: string;
  baseline: number | null;
  candidate: number | null;
  decision: string;
  created_at: string;
}

export interface AgentProfile {
  agent: { id: string; name: string; role: string };
  skills: Skill[];
  skills_waiting: Skill[];
  lessons_active: Lesson[];
  lessons_pending: Lesson[];
  eval_cases: number;
  recent_evaluations: EvalRun[];
  usage_today: { tool_calls: number; chat_turns: number; daily_tool_calls: number | null; daily_chat_turns: number | null };
}

export interface StudyPlan {
  agent_id: string;
  topics: string[];
  interval_hours: number;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string;
}

// Projects, knowledge graph, study digest (app/projects/core.py, app/graph/core.py, app/learning/study.py).

export interface Project {
  id: string;
  name: string;
  description: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
}

export type EntryKind = 'decision' | 'task' | 'note' | 'status';

export interface BriefItem {
  id: string;
  text: string;
  author: 'user' | 'agent';
  at: string;
}

export interface ProjectBrief {
  project: Project;
  current_status: BriefItem[];
  open_tasks: BriefItem[];
  recent_decisions: BriefItem[];
  recent_notes: BriefItem[];
  done_tasks: number;
}

export type EntityType = 'person' | 'project' | 'organization' | 'place' | 'topic' | 'other';

export interface GraphEntity {
  id: string;
  type: EntityType;
  name: string;
  description: string;
  author: 'user' | 'agent';
  agent_id: string | null;
  updated_at: string;
}

export interface GraphRelation {
  id: string;
  subject: string;
  subject_type: EntityType;
  predicate: string;
  object: string;
  object_type: EntityType;
  note: string;
  author: 'user' | 'agent';
}

export interface GraphLookup {
  matches: { entity: GraphEntity; relations: GraphRelation[] }[];
}

export interface StudyRun {
  id: string;
  agent_id: string;
  started_at: string;
  status: string;
  summary: string;
  detail: Record<string, unknown>;
}

export interface StudyDigest {
  since: string;
  runs: StudyRun[];
  lessons_to_review: number;
  approvals_waiting: number;
  skills_promoted: number;
  skills_rejected: number;
}

export interface ControlState {
  paused: boolean;
  [key: string]: unknown;
}

export interface AgentBudget {
  agent_id: string;
  day: string;
  tool_calls: number;
  chat_turns: number;
  daily_tool_calls: number | null;
  daily_chat_turns: number | null;
}

export interface BudgetLimits {
  daily_tool_calls: number | null;
  daily_chat_turns: number | null;
}

export interface StandingApproval {
  id: string;
  agent_id: string;
  tool_name: string;
  workspace_id: string | null;
  path_prefix: string | null;
  created_at: string;
  expires_at: string | null;
  active: boolean;
}

export interface NewStandingApproval {
  agent_id: string;
  tool_name: string;
  path_prefix?: string;
  expires_in_days?: number;
}

export interface Routine {
  id: string;
  name: string;
  prompt: string;
  agent_id: string | null;
  time_of_day: string;
  days: number[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
  last_run_at: string | null;
  last_status: 'ok' | 'error' | 'refused' | null;
  last_error: string | null;
  last_conversation_id: string | null;
  next_run_at: string;
}

export interface RoutineInput {
  name: string;
  prompt: string;
  agent_id: string | null;
  time_of_day: string;
  days: number[];
  enabled?: boolean;
}
