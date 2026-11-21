import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { N8nWorkflow } from '../core/models';
import { N8nSection, validateWorkflow } from './n8n-section';

function wf(extra: Partial<N8nWorkflow> = {}): N8nWorkflow {
  return {
    id: 'w1', name: 'send-report', description: 'يرسل التقرير', webhook_path: 'hooks/report', method: 'POST',
    enabled: true, created_at: '2026-11-21T00:00:00Z', updated_at: '2026-11-21T00:00:00Z', ...extra,
  };
}

function setup(healthy = true) {
  const requests: string[] = [];
  const bodies: Record<string, unknown> = {};
  let list = [wf()];
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const key = `${String(args?.['method'])} ${String(args?.['path'])}`;
    requests.push(key);
    bodies[key] = args?.['body'];
    const ok = (body: unknown, status = 200) => ({ status, body }) as T;
    if (key === 'GET /api/v1/n8n/status') {
      return ok(healthy ? { ok: true, base_url: 'http://127.0.0.1:5678', status_code: 200, error: null }
        : { ok: false, base_url: 'http://127.0.0.1:5678', error: 'n8n is not reachable.' });
    }
    if (key === 'GET /api/v1/n8n/workflows') return ok({ workflows: list.map((w) => ({ ...w })) });
    if (key === 'POST /api/v1/n8n/workflows') {
      const created = wf({ id: 'w2', ...(args?.['body'] as object) });
      list = [...list, created];
      return ok({ ...created }, 201);
    }
    if (key === 'PATCH /api/v1/n8n/workflows/w1') {
      list = list.map((w) => (w.id === 'w1' ? { ...w, ...(args?.['body'] as object) } : w));
      return ok({ ...list[0] });
    }
    if (key === 'DELETE /api/v1/n8n/workflows/w1') { list = []; return ok(null, 204); }
    return { status: 404, body: { detail: 'nope' } } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(N8nSection);
  return { fixture, page: fixture.componentInstance, requests, bodies, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  fixture.detectChanges();
  await fixture.whenStable();
  await new Promise((r) => setTimeout(r, 0));
  fixture.detectChanges();
}

describe('N8nSection', () => {
  it('shows status, the approval note and the list', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    expect(el.textContent).toContain('متصل');
    expect(el.textContent).toContain('127.0.0.1:5678');
    expect(el.textContent).toContain('يطلب موافقتك');
    expect(el.textContent).toContain('send-report');
    expect(el.textContent).toContain('POST /webhook/hooks/report');
  });

  it('shows a disconnected status with the reason', async () => {
    const { fixture, el } = setup(false);
    await settle(fixture);
    expect(el.textContent).toContain('غير متصل');
    expect(el.textContent).toContain('not reachable');
  });

  it('validates before sending and creates a workflow', async () => {
    const { fixture, page, requests, bodies } = setup();
    await settle(fixture);
    page.openNew();
    page.name.set('Bad Name');
    page.path.set('x');
    await page.save();
    expect(page.formError()).toContain('الاسم');
    page.name.set('new-flow');
    page.path.set('../x');
    await page.save();
    expect(page.formError()).toContain('المسار');
    expect(requests).not.toContain('POST /api/v1/n8n/workflows');
    page.path.set('flows/new');
    page.method.set('GET');
    await page.save();
    await settle(fixture);
    expect(bodies['POST /api/v1/n8n/workflows']).toEqual({
      name: 'new-flow', description: '', webhook_path: 'flows/new', method: 'GET', enabled: true,
    });
    expect(page.workflows().map((w) => w.name)).toEqual(['send-report', 'new-flow']);
    expect(page.editing()).toBeNull();
  });

  it('edits, disables and deletes with a confirm click', async () => {
    const { fixture, page, bodies } = setup();
    await settle(fixture);
    page.edit(page.workflows()[0]);
    page.description.set('وصف جديد');
    await page.save();
    expect(bodies['PATCH /api/v1/n8n/workflows/w1']).toMatchObject({ description: 'وصف جديد' });
    await page.toggle(page.workflows()[0]);
    expect(page.workflows()[0].enabled).toBe(false);
    await page.remove(page.workflows()[0]);
    expect(page.workflows().length).toBe(1);
    await page.remove(page.workflows()[0]);
    expect(page.workflows().length).toBe(0);
  });

  it('validateWorkflow rejects traversal and long text', () => {
    expect(validateWorkflow('ok', '', 'a/b_c-d').ok).toBe(true);
    expect(validateWorkflow('ok', '', '/abs').ok).toBe(false);
    expect(validateWorkflow('ok', '', 'a//b').ok).toBe(false);
    expect(validateWorkflow('ok', 'x'.repeat(301), 'a').ok).toBe(false);
  });
});
