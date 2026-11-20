// Test data and a fake server for the team pages.

import { Agent, AgentProfile, AgentTemplate, Lesson, Skill, StudyPlan } from '../core/models';
import { Invoke } from '../core/api';
import { summary } from '../dashboard/summary.testing';

export function agentRow(id: string, name: string, extra: Partial<Agent> = {}): Agent {
  return {
    id, name, role: 'role', enabled: true, description: '', system_prompt: '', model: 'qwen3.5:9b',
    created_at: '2026-11-20T10:00:00Z', updated_at: '2026-11-20T10:00:00Z', ...extra,
  };
}

export const TEMPLATE: AgentTemplate = {
  id: 'researcher', name: 'Research Agent', title_ar: 'الباحث', role: 'Research', description: 'يبحث ويلخص.',
  tools: ['research', 'record_lesson', 'project_brief'], daily_tool_calls: 50, daily_chat_turns: null, notes: ['أضف موافقة دائمة للبحث إن أردت.'],
};

export function lesson(id: string, status: Lesson['status'], extra: Partial<Lesson> = {}): Lesson {
  return { id, agent_id: 'a1', text: `درس ${id}`, topic: null, source: 'self', status, uses: 0, created_at: '2026-11-21T09:00:00Z', ...extra };
}

export function skill(id: string, status: Skill['status']): Skill {
  return { id, agent_id: 'a1', name: `skill-${id}`, version: 2, description: 'وصف', steps: '1. خطوة', status, source: 'self', created_at: '2026-11-21T09:00:00Z' };
}

export function profile(extra: Partial<AgentProfile> = {}): AgentProfile {
  return {
    agent: { id: 'a1', name: 'الباحث', role: 'Research' },
    skills: [skill('s1', 'active')],
    skills_waiting: [skill('s2', 'candidate')],
    lessons_active: [lesson('l1', 'active', { uses: 3 })],
    lessons_pending: [lesson('l2', 'pending')],
    eval_cases: 2,
    recent_evaluations: [{ id: 'e1', skill_id: 's1', baseline: 0.5, candidate: 1, decision: 'promoted', created_at: '2026-11-21T08:00:00Z' }],
    usage_today: { tool_calls: 5, chat_turns: 2, daily_tool_calls: 50, daily_chat_turns: null },
    ...extra,
  };
}

/** A tiny stateful server: records requests, answers the team routes. */
export function fakeServer(state: { agents: Agent[]; plan?: StudyPlan | null; members?: string[]; profile?: AgentProfile }) {
  const requests: { method: string; path: string; body: unknown }[] = [];
  const invoke: Invoke = async <T>(_command: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    const body = args?.['body'];
    requests.push({ method, path, body });
    const ok = (data: unknown) => ({ status: 200, body: data }) as T;
    // Fresh copies, like a real server: the page must not depend on shared arrays.
    if (path === '/api/v1/agents' && method === 'GET') {
      return ok({ agents: state.agents.map((a) => ({ ...a })), count: state.agents.length });
    }
    if (path.startsWith('/api/v1/analytics/summary')) return ok(summary({ agents: [] }));
    if (path === '/api/v1/agent-templates') return ok({ templates: [TEMPLATE], count: 1 });
    if (path === '/api/v1/agents/hire') {
      const hired = agentRow('new', String((body as { name?: string }).name ?? 'x'));
      state.agents.push(hired);
      return { status: 201, body: { agent: hired, template_id: 'researcher', granted_tools: TEMPLATE.tools, budget: null, next_steps: TEMPLATE.notes } } as T;
    }
    if (path.endsWith('/profile')) return ok(state.profile ?? profile());
    if (path.endsWith('/study-plan') && method === 'GET') {
      return (state.plan ? ok(state.plan) : { status: 404, body: { detail: 'This agent has no study plan.' } }) as T;
    }
    if (path.endsWith('/study-plan') && method === 'PUT') {
      const b = body as { topics: string[]; interval_hours: number };
      state.plan = { agent_id: 'a1', topics: b.topics, interval_hours: b.interval_hours, enabled: true, last_run_at: null, next_run_at: '2026-11-22T10:00:00Z' };
      return ok(state.plan);
    }
    if (path.endsWith('/team') && method === 'GET') return ok({ agent_id: 'a1', member_ids: state.members ?? [] });
    if (path.endsWith('/team') && method === 'PUT') {
      state.members = (body as { member_ids: string[] }).member_ids;
      return ok({ agent_id: 'a1', member_ids: state.members });
    }
    if (method === 'PATCH' && path.startsWith('/api/v1/agents/')) {
      const target = state.agents.find((a) => path.endsWith(a.id));
      if (target) target.enabled = (body as { enabled: boolean }).enabled;
      return ok(target);
    }
    if (method === 'PATCH') return ok({});
    return { status: 404, body: { detail: 'not found' } } as T;
  };
  return { requests, invoke };
}
