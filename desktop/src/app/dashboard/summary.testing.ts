// Test data only: a summary shaped like the server's (app/analytics.py).

import { AgentActivity, AnalyticsSummary } from '../core/models';

export const NOW = new Date('2026-11-21T10:00:00Z');

export function agent(name: string, extra: Partial<AgentActivity> = {}): AgentActivity {
  return {
    id: `id-${name}`, name, role: 'role', enabled: true, members: [], chat_turns: 4, tool_actions: 2,
    lessons_pending: 0, skills_active: 1, study_runs: 0, last_active_at: '2026-11-21T09:58:00Z', ...extra,
  };
}

export function summary(extra: Partial<AnalyticsSummary> = {}): AnalyticsSummary {
  return {
    range: '7d',
    since: '2026-11-14T10:00:00Z',
    generated_at: NOW.toISOString(),
    paused: false,
    kpis: {
      chat_turns: { value: 12, previous: 8 },
      conversations: { value: 3, previous: 3 },
      tool_actions: { value: 4, completed: 3, failed: 1, success_rate: 0.75 },
      approvals_pending: { value: 2, oldest_at: '2026-11-21T09:46:00Z' },
      lessons_new: { value: 4, pending: 1 },
      budget: { tool_calls_used: 30, tool_calls_limit: 120, chat_turns_used: 0, chat_turns_limit: 0 },
    },
    agents: [agent('مدير الفريق'), agent('الباحث', { last_active_at: '2026-11-20T10:00:00Z' })],
    systems: {
      memories: { count: 20, added: 2 },
      knowledge: { documents: 5, chunks: 80 },
      projects: { count: 1, entries: 9 },
      graph: { entities: 12, relations: 7 },
      tools: { denied: 1 },
    },
    queue: [
      { id: 'q1', tool: 'run_code', agent_id: 'id-الباحث', agent_name: 'الباحث', description: 'تحليل ملف', risk: 'medium', status: 'PENDING', created_at: '2026-11-21T09:58:00Z' },
      { id: 'q2', tool: 'write_file', agent_id: null, agent_name: null, description: '', risk: 'low', status: 'COMPLETED', created_at: '2026-11-21T07:00:00Z' },
    ],
    trend: {
      step: 'day',
      starts: Array.from({ length: 7 }, (_, i) => new Date(Date.UTC(2026, 10, 14 + i, 10)).toISOString()),
      chat_turns: [1, 2, 0, 3, 1, 2, 3], tool_completed: [0, 1, 0, 1, 0, 1, 0], tool_failed: [0, 0, 0, 1, 0, 0, 0],
    },
    tools: [
      { tool: 'run_code', actions: 3, failed: 1, denied: 0 },
      { tool: 'write_file', actions: 1, failed: 0, denied: 1 },
    ],
    learning: { lessons_active: 3, lessons_pending: 1, skills_active: 2, skills_candidate: 1, study_runs: 2, memories_added: 2 },
    ...extra,
  };
}
