import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { Routine } from '../core/models';
import { RoutinesPage } from './routines-page';

function routine(extra: Partial<Routine> = {}): Routine {
  return {
    id: 'r1', name: 'ملخص', prompt: 'p', agent_id: null, time_of_day: '08:00', days: [], enabled: true,
    created_at: '2026-11-21T00:00:00Z', updated_at: '2026-11-21T00:00:00Z', last_run_at: null,
    last_status: null, last_error: null, last_conversation_id: null, next_run_at: '2026-11-22T08:00:00+02:00', ...extra,
  };
}

function setup() {
  const requests: string[] = [];
  const bodies: Record<string, unknown> = {};
  let list = [routine()];
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    const key = `${method} ${path}`;
    requests.push(key);
    bodies[key] = args?.['body'];
    const ok = (body: unknown, status = 200) => ({ status, body }) as T;
    if (key === 'GET /api/v1/routines') return ok({ routines: list.map((r) => ({ ...r })) });
    if (key === 'GET /api/v1/agents') return ok({ agents: [{ id: 'a1', name: 'الباحث', role: 'r', enabled: true }], count: 1 });
    if (key === 'POST /api/v1/routines') {
      const created = routine({ id: 'r2', ...(args?.['body'] as object) });
      list = [...list, created];
      return ok({ ...created }, 201);
    }
    if (key === 'PATCH /api/v1/routines/r1') {
      list = list.map((r) => (r.id === 'r1' ? { ...r, ...(args?.['body'] as object) } : r));
      return ok({ ...list[0] });
    }
    if (key === 'POST /api/v1/routines/r1/run') {
      list = list.map((r) => ({ ...r, last_status: 'ok' as const, last_conversation_id: 'c1' }));
      return ok({ ...list[0] });
    }
    if (key === 'DELETE /api/v1/routines/r1') { list = []; return ok(null, 204); }
    return { status: 404, body: { detail: 'nope' } } as T;
  };
  TestBed.configureTestingModule({ providers: [provideRouter([]), { provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(RoutinesPage);
  return { fixture, page: fixture.componentInstance, requests, bodies, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  fixture.detectChanges();
  await fixture.whenStable();
  await new Promise((r) => setTimeout(r, 0));
  fixture.detectChanges();
}

describe('RoutinesPage', () => {
  it('lists routines with agent name and time', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    expect(el.textContent).toContain('ملخص');
    expect(el.textContent).toContain('08:00');
    expect(el.textContent).toContain('المساعد الافتراضي');
    expect(el.textContent).toContain('كل يوم');
  });

  it('creates from the preset', async () => {
    const { fixture, page, bodies, el } = setup();
    await settle(fixture);
    page.openNew();
    page.preset();
    page.toggleDay(0);
    await settle(fixture);
    await page.save();
    await settle(fixture);
    const body = bodies['POST /api/v1/routines'] as { name: string; time_of_day: string; days: number[]; agent_id: null };
    expect(body.name).toBe('ملخص الصبح');
    expect(body.time_of_day).toBe('08:00');
    expect(body.days).toEqual([0]);
    expect(body.agent_id).toBeNull();
    expect(page.routines().length).toBe(2);
    expect(el.textContent).toContain('ملخص الصبح');
  });

  it('refuses an empty form without calling the API', async () => {
    const { fixture, page, requests } = setup();
    await settle(fixture);
    page.openNew();
    await page.save();
    expect(page.formError()).not.toBe('');
    expect(requests).not.toContain('POST /api/v1/routines');
  });

  it('toggles, runs and links to the conversation', async () => {
    const { fixture, page, bodies, el } = setup();
    await settle(fixture);
    await page.toggle(page.routines()[0]);
    expect(bodies['PATCH /api/v1/routines/r1']).toEqual({ enabled: false });
    await page.run(page.routines()[0]);
    await settle(fixture);
    expect(page.routines()[0].last_status).toBe('ok');
    expect(el.querySelector('a[href="/chat/c1"]')).not.toBeNull();
  });

  it('deletes only after a second click', async () => {
    const { fixture, page, requests } = setup();
    await settle(fixture);
    await page.remove(page.routines()[0]);
    expect(requests).not.toContain('DELETE /api/v1/routines/r1');
    await page.remove(page.routines()[0]);
    expect(requests).toContain('DELETE /api/v1/routines/r1');
    expect(page.routines()).toEqual([]);
  });
});
