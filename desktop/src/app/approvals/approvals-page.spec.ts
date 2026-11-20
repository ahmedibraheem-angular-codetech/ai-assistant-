import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { ApprovalsStore } from '../core/approvals-store';
import { Approval } from '../core/models';
import { ApprovalsPage } from './approvals-page';
import { approval } from './approval.testing';

type Handler = (method: string, path: string) => { status: number; body: unknown };

function setup(pending: Approval[], extra: Handler = () => ({ status: 404, body: { detail: 'nope' } })) {
  const requests: string[] = [];
  let queue = [...pending];
  const invoke: Invoke = async <T>(_command: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    requests.push(`${method} ${path}`);
    if (path === '/api/v1/approvals') return { status: 200, body: { approvals: queue, count: queue.length } } as T;
    if (path === '/api/v1/agents') return { status: 200, body: { agents: [{ id: 'agent-1', name: 'الباحث', role: 'r', enabled: true }], count: 1 } } as T;
    const decided = path.match(/approvals\/(.+)\/(approve|reject)$/);
    if (decided && method === 'POST') {
      const response = extra(method, path);
      if (response.status === 200) queue = queue.filter((item) => item.id !== decided[1]);
      return response as T;
    }
    return extra(method, path) as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(ApprovalsPage);
  return { fixture, page: fixture.componentInstance, requests, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

afterEach(() => TestBed.inject(ApprovalsStore).stop());

describe('ApprovalsPage', () => {
  it('lists pending requests with agent names and shows the first in detail', async () => {
    const { fixture, el } = setup([approval(), approval({ id: 'a2', tool_name: 'write_file', risk: 'low' })]);
    await settle(fixture);
    expect(el.querySelectorAll('.item')).toHaveLength(2);
    expect(el.querySelector('.item')?.textContent).toContain('الباحث');
    expect(el.querySelector('.detail h2')?.textContent).toContain('تحليل ملف المبيعات');
    expect(el.querySelector('.detail pre')?.textContent).toContain('print(1)');
  });

  it('approve needs a second, explicit click before anything runs', async () => {
    const { fixture, page, el, requests } = setup([approval()], () => ({
      status: 200, body: { ...approval(), status: 'COMPLETED', output: { stdout: '1' } },
    }));
    await settle(fixture);
    el.querySelector<HTMLButtonElement>('.actions .primary')?.click();
    fixture.detectChanges();
    expect(requests.some((r) => r.includes('/approve'))).toBe(false);
    expect(el.querySelector('.confirm')?.textContent).toContain('run_code');

    await page.approve();
    await settle(fixture);
    expect(requests).toContain('POST /api/v1/approvals/a1/approve');
    expect(el.querySelector('.banner.ok')?.textContent).toContain('نُفذت');
    expect(el.querySelector('.banner pre')?.textContent).toContain('stdout');
    expect(el.querySelectorAll('.item')).toHaveLength(0);
  });

  it('reject sends one request and says nothing will run', async () => {
    const { fixture, page, el, requests } = setup([approval()], () => ({ status: 200, body: { ...approval(), status: 'REJECTED' } }));
    await settle(fixture);
    await page.reject();
    await settle(fixture);
    expect(requests).toContain('POST /api/v1/approvals/a1/reject');
    expect(el.querySelector('.banner')?.textContent).toContain('لن يُنفذ شيء');
  });

  it('a tampered request cannot be approved, only rejected', async () => {
    const bad = approval();
    bad.review = { ...bad.review, verified: false, warnings: ['The stored operation does not match its fingerprint.'] };
    const { fixture, page, el, requests } = setup([bad]);
    await settle(fixture);
    expect(el.querySelector<HTMLButtonElement>('.actions .primary')?.disabled).toBe(true);
    expect(el.querySelector('.warnings')?.textContent).toContain('fingerprint');
    page.askConfirm();
    await page.approve();
    expect(requests.some((r) => r.includes('/approve'))).toBe(false);
  });

  it('shows the server refusal (409) in plain words', async () => {
    const { fixture, page, el } = setup([approval()], () => ({
      status: 409, body: { detail: { message: 'This request is no longer pending.', status: 'EXPIRED' } },
    }));
    await settle(fixture);
    page.askConfirm();
    await page.approve();
    await settle(fixture);
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('no longer pending');
  });

  it('history tab loads decided requests', async () => {
    const { fixture, page, el, requests } = setup([], (_m, path) =>
      path.endsWith('/history')
        ? { status: 200, body: { approvals: [approval({ status: 'COMPLETED', result: 'ok', decided_at: '2026-11-21T09:55:00Z' })], count: 1 } }
        : { status: 404, body: {} },
    );
    await settle(fixture);
    expect(el.querySelector('.empty')?.textContent).toContain('لا شيء بانتظارك');
    page.showTab('history');
    await settle(fixture);
    expect(requests).toContain('GET /api/v1/approvals/history');
    expect(el.querySelector('.item .badge')?.textContent).toContain('نُفذت');
    expect(el.querySelector('.actions')).toBeNull();
  });
});
