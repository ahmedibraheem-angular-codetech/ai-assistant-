import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';

import { TAURI_INVOKE } from '../core/api';
import { TeamPage } from './team-page';
import { TEMPLATE, agentRow, fakeServer } from './team.testing';

function setup(agents = [agentRow('a1', 'الباحث'), agentRow('a2', 'الكاتب', { enabled: false })]) {
  const server = fakeServer({ agents });
  TestBed.configureTestingModule({ providers: [provideRouter([]), { provide: TAURI_INVOKE, useValue: server.invoke }] });
  const fixture = TestBed.createComponent(TeamPage);
  return { fixture, page: fixture.componentInstance, server, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

describe('TeamPage', () => {
  it('lists agents, running ones first', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    const cards = el.querySelectorAll('.agent');
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('الباحث');
    expect(cards[1].classList).toContain('off');
  });

  it('hires from a template with the chosen name and budget, then lists the new agent', async () => {
    const { fixture, page, server, el } = setup();
    await settle(fixture);
    await page.openHiring();
    fixture.detectChanges();
    expect(el.querySelector('.template')?.textContent).toContain('الباحث');

    page.choose(TEMPLATE);
    page.name.set('باحث السوق');
    page.instructions.set('ركز على مصر');
    await page.hire();
    await settle(fixture);

    const hire = server.requests.find((r) => r.path === '/api/v1/agents/hire');
    expect(hire?.body).toEqual({ template_id: 'researcher', name: 'باحث السوق', extra_instructions: 'ركز على مصر', apply_budget: true });
    expect(el.querySelector('.banner.ok')?.textContent).toContain('باحث السوق');
    expect(el.querySelectorAll('.agent')).toHaveLength(3);
  });

  it('opens an agent profile', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    const router = TestBed.inject(Router);
    const spy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    el.querySelector<HTMLButtonElement>('.agent')?.click();
    expect(spy).toHaveBeenCalledWith(['/team', 'a1']);
  });

  it('invites hiring when the team is empty', async () => {
    const { fixture, el } = setup([]);
    await settle(fixture);
    expect(el.querySelector('.empty')?.textContent).toContain('لا يوجد وكلاء بعد');
  });

  it('shows what the team learned in the last 24 hours', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    const digest = el.querySelector('.digest');
    expect(digest?.textContent).toContain('2 درس للمراجعة');
    expect(digest?.querySelector('.digest-run')?.textContent).toContain('الباحث');
    expect(digest?.textContent).toContain('ذاكر إعلانات فيسبوك');
  });
});
