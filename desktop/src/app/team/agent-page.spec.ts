import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { TAURI_INVOKE } from '../core/api';
import { StudyPlan } from '../core/models';
import { AgentPage } from './agent-page';
import { agentRow, fakeServer } from './team.testing';

function setup(plan: StudyPlan | null = null) {
  const state = { agents: [agentRow('a1', 'الباحث'), agentRow('a2', 'الكاتب')], plan, members: [] as string[] };
  const server = fakeServer(state);
  TestBed.configureTestingModule({ providers: [provideRouter([]), { provide: TAURI_INVOKE, useValue: server.invoke }] });
  const fixture = TestBed.createComponent(AgentPage);
  fixture.componentRef.setInput('id', 'a1');
  return { fixture, page: fixture.componentInstance, server, state, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

describe('AgentPage', () => {
  it('shows the profile: usage, what waits for review, skills, lessons, evaluations', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    expect(el.querySelector('h1')?.textContent).toContain('الباحث');
    expect(el.querySelector('.stats')?.textContent).toContain('5 / 50');
    expect(el.querySelector('.attention h2')?.textContent).toContain('2');
    expect(el.querySelector('.attention')?.textContent).toContain('درس l2');
    expect(el.textContent).toContain('skill-s1');
    expect(el.textContent).toContain('قبل 50% ← بعد 100%');
    expect(el.textContent).toContain('لا توجد خطة');
  });

  it('approving a pending lesson sends status active and reloads', async () => {
    const { fixture, page, server } = setup();
    await settle(fixture);
    await page.lesson(page.profile()!.lessons_pending[0], 'active');
    expect(server.requests).toContainEqual({ method: 'PATCH', path: '/api/v1/lessons/l2', body: { status: 'active' } });
    expect(page.notice()).toContain('اعتمدت');
    expect(server.requests.filter((r) => r.path.endsWith('/profile')).length).toBe(2);
  });

  it('activating a waiting skill sends status active', async () => {
    const { fixture, page, server } = setup();
    await settle(fixture);
    await page.skill(page.profile()!.skills_waiting[0], 'active');
    expect(server.requests).toContainEqual({ method: 'PATCH', path: '/api/v1/skills/s2', body: { status: 'active' } });
  });

  it('stopping the agent patches enabled=false', async () => {
    const { fixture, page, server, state } = setup();
    await settle(fixture);
    await page.toggleEnabled();
    expect(server.requests).toContainEqual({ method: 'PATCH', path: '/api/v1/agents/a1', body: { enabled: false } });
    expect(state.agents[0].enabled).toBe(false);
  });

  it('saves a study plan from lines of topics', async () => {
    const { fixture, page, server, el } = setup();
    await settle(fixture);
    page.topicsText.set('إعلانات\nكتابة');
    page.interval.set(48);
    await page.savePlan();
    await settle(fixture);
    expect(server.requests).toContainEqual({
      method: 'PUT', path: '/api/v1/agents/a1/study-plan', body: { topics: ['إعلانات', 'كتابة'], interval_hours: 48, enabled: true },
    });
    expect(el.textContent).toContain('كل يومين'.replace('يومين', '2 أيام'));
  });

  it('refuses an empty plan without calling the server', async () => {
    const { fixture, page, server } = setup();
    await settle(fixture);
    page.topicsText.set('   ');
    await page.savePlan();
    expect(page.error()).toContain('موضوعاً');
    expect(server.requests.some((r) => r.method === 'PUT')).toBe(false);
  });

  it('adds a team member with the full new list', async () => {
    const { fixture, page, server } = setup();
    await settle(fixture);
    await page.toggleMember('a2');
    expect(server.requests).toContainEqual({ method: 'PUT', path: '/api/v1/agents/a1/team', body: { member_ids: ['a2'] } });
    expect(page.isMember('a2')).toBe(true);
  });
});
