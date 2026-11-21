import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { AgentBudget, StandingApproval } from '../core/models';
import { SettingsPage } from './settings-page';

const AGENTS = [{ id: 'a1', name: 'الباحث', role: 'r', enabled: true }, { id: 'a2', name: 'الكاتب', role: 'r', enabled: true }];
const BASE = '/api/v1/control/agents';

function standing(extra: Partial<StandingApproval> = {}): StandingApproval {
  return {
    id: 's1', agent_id: 'a1', tool_name: 'read_file', workspace_id: null, path_prefix: 'docs/',
    created_at: '2026-11-21T00:00:00Z', expires_at: null, active: true, ...extra,
  };
}

function setup(opts: { fail?: Record<string, { status: number; body: unknown }> } = {}) {
  const requests: string[] = [];
  const bodies: Record<string, unknown> = {};
  let paused = false;
  const budgets: Record<string, AgentBudget> = {
    a1: { agent_id: 'a1', day: '2026-11-21', tool_calls: 8, chat_turns: 2, daily_tool_calls: 10, daily_chat_turns: null },
    a2: { agent_id: 'a2', day: '2026-11-21', tool_calls: 0, chat_turns: 0, daily_tool_calls: null, daily_chat_turns: null },
  };
  let list = [standing()];
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    const key = `${method} ${path}`;
    requests.push(key);
    bodies[key] = args?.['body'];
    const forced = opts.fail?.[key];
    if (forced) return forced as T;
    const ok = (body: unknown, status = 200) => ({ status, body }) as T;
    if (key === 'GET /api/v1/control') return ok({ paused });
    if (key === 'POST /api/v1/control/pause') { paused = true; return ok({ paused }); }
    if (key === 'POST /api/v1/control/resume') { paused = false; return ok({ paused }); }
    if (key === 'GET /api/v1/agents') return ok({ agents: [...AGENTS], count: 2 });
    const m = path.match(/control\/agents\/(.+)\/budget$/);
    if (m) {
      if (method === 'GET') return ok({ ...budgets[m[1]] });
      if (method === 'PUT') { budgets[m[1]] = { ...budgets[m[1]], ...(args?.['body'] as object) }; return ok({ ...budgets[m[1]] }); }
      if (method === 'DELETE') { budgets[m[1]] = { ...budgets[m[1]], daily_tool_calls: null, daily_chat_turns: null }; return ok(null, 204); }
    }
    if (path === '/api/v1/permissions/standing-approvals') {
      if (method === 'GET') return ok({ standing_approvals: [...list], count: list.length });
      list = [...list, standing({ id: 's2', tool_name: (args?.['body'] as { tool_name: string }).tool_name })];
      return ok(list[list.length - 1], 201);
    }
    if (method === 'DELETE' && path.includes('standing-approvals/')) { list = list.filter((s) => !path.endsWith(s.id)); return ok(null, 204); }
    return { status: 404, body: { detail: 'nope' } } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(SettingsPage);
  return { fixture, page: fixture.componentInstance, requests, bodies, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  fixture.detectChanges();
  await fixture.whenStable();
  for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r));
  fixture.detectChanges();
}

describe('SettingsPage', () => {
  it('shows loading first, then state, usage vs limits and agent names', async () => {
    const { fixture, el } = setup();
    fixture.detectChanges();
    expect(el.querySelector('[role="status"]')?.textContent).toContain('جارٍ التحميل');
    await settle(fixture);
    expect(el.querySelector('.badge')?.textContent).toContain('يعمل');
    const agents = el.querySelectorAll('.agent');
    expect(agents).toHaveLength(2);
    expect(agents[0].textContent).toContain('الباحث');
    expect(agents[0].textContent).toContain('8 / 10');
    expect(agents[0].textContent).toContain('2 / بلا حد');
    expect(agents[1].textContent).not.toContain('إزالة الحدود');
    expect(el.querySelector('.item')?.textContent).toContain('الباحث');
    expect(el.querySelector('#s-standing')?.parentElement?.textContent).toContain('أدوات القراءة فقط');
  });

  it('pause then resume', async () => {
    const { fixture, page, el, requests, bodies } = setup();
    await settle(fixture);
    await page.togglePause();
    await settle(fixture);
    expect(bodies['POST /api/v1/control/pause']).toEqual({ reason: 'من الإعدادات' });
    expect(el.querySelector('.badge')?.textContent).toContain('متوقف');
    await page.togglePause();
    await settle(fixture);
    expect(requests).toContain('POST /api/v1/control/resume');
    expect(el.querySelector('.badge')?.textContent).toContain('يعمل');
  });

  it('invalid limits are not sent; valid ones are saved', async () => {
    const { fixture, page, el, requests, bodies } = setup();
    await settle(fixture);
    page.edit('a2');
    page.toolsText.set('0');
    await page.saveBudget();
    await settle(fixture);
    expect(requests.some((r) => r.startsWith('PUT'))).toBe(false);
    expect(el.querySelector('#s-budgets')?.parentElement?.querySelector('[role="alert"]')).not.toBeNull();
    page.toolsText.set('25');
    await page.saveBudget();
    await settle(fixture);
    expect(bodies[`PUT ${BASE}/a2/budget`]).toEqual({ daily_tool_calls: 25, daily_chat_turns: null });
    expect(el.querySelectorAll('.agent')[1].textContent).toContain('0 / 25');
  });

  it('remove limits sends DELETE and refreshes', async () => {
    const { fixture, page, el, requests } = setup();
    await settle(fixture);
    await page.removeLimits('a1');
    await settle(fixture);
    expect(requests).toContain(`DELETE ${BASE}/a1/budget`);
    expect(el.querySelectorAll('.agent')[0].textContent).toContain('8 / بلا حد');
  });

  it('adds and revokes a standing approval', async () => {
    const { fixture, page, el, requests, bodies } = setup();
    await settle(fixture);
    page.saAgent.set('a2');
    page.saTool.set('list_files');
    page.saDays.set('7');
    await page.addStanding();
    await settle(fixture);
    expect(bodies['POST /api/v1/permissions/standing-approvals']).toEqual({ agent_id: 'a2', tool_name: 'list_files', expires_in_days: 7 });
    expect(el.querySelectorAll('.item')).toHaveLength(2);
    await page.revoke('s1');
    await settle(fixture);
    expect(requests).toContain('DELETE /api/v1/permissions/standing-approvals/s1');
    expect(el.querySelectorAll('.item')).toHaveLength(1);
  });

  it('shows the server refusal for a non-read tool inline', async () => {
    const { fixture, page, el } = setup({
      fail: { 'POST /api/v1/permissions/standing-approvals': { status: 422, body: { detail: 'Only read tools can get a standing approval.' } } },
    });
    await settle(fixture);
    page.saAgent.set('a1');
    page.saTool.set('write_file');
    await page.addStanding();
    await settle(fixture);
    expect(el.querySelector('#s-standing')?.parentElement?.querySelector('[role="alert"]')?.textContent).toContain('read tools');
    expect(el.querySelectorAll('.item')).toHaveLength(1);
  });

  it('an error in one section does not hide the others', async () => {
    const { fixture, el } = setup({ fail: { 'GET /api/v1/control': { status: 500, body: { detail: 'boom' } } } });
    await settle(fixture);
    expect(el.querySelector('#s-control')?.parentElement?.querySelector('[role="alert"]')).not.toBeNull();
    expect(el.querySelectorAll('.agent')).toHaveLength(2);
  });

  it('shows an empty state when there are no standing approvals', async () => {
    const empty = setup({ fail: { 'GET /api/v1/permissions/standing-approvals': { status: 200, body: { standing_approvals: [], count: 0 } } } });
    await settle(empty.fixture);
    expect(empty.el.querySelector('.empty')?.textContent).toContain('لا توجد موافقات دائمة');
  });
});
