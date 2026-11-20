import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { LayoutService } from '../core/layout';
import { AnalyticsSummary } from '../core/models';
import { DashboardPage } from './dashboard-page';
import { summary } from './summary.testing';

function setup(handler: (method: string, path: string) => { status: number; body: unknown }) {
  const requests: string[] = [];
  const invoke: Invoke = async <T>(command: string, args?: Record<string, unknown>) => {
    if (command === 'api_health') return true as T;
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    requests.push(`${method} ${path}`);
    return handler(method, path) as T;
  };
  TestBed.configureTestingModule({ providers: [provideRouter([]), { provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(DashboardPage);
  return { fixture, page: fixture.componentInstance, requests, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

describe('DashboardPage', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('loads the summary and shows KPIs, the map, the queue and learning', async () => {
    const { fixture, el, requests } = setup(() => ({ status: 200, body: summary() }));
    await settle(fixture);

    expect(requests[0]).toBe('GET /api/v1/analytics/summary?range=7d');
    expect(el.querySelectorAll('.kpi')).toHaveLength(6);
    expect(el.querySelector('.kpi .big')?.textContent).toContain('12');
    expect(el.querySelectorAll('.node')).toHaveLength(7);
    expect(el.querySelectorAll('.queue .row')).toHaveLength(2);
    expect(el.querySelector('.queue .badge')?.textContent).toContain('بانتظار موافقة');
    expect(el.querySelectorAll('.bar')).toHaveLength(2);
    expect(el.querySelector('.tiles')?.textContent).toContain('دروس معتمدة');
  });

  it('switches the range and asks the server again', async () => {
    const { fixture, page, requests } = setup((_m, path) => ({
      status: 200, body: summary({ range: path.endsWith('today') ? 'today' : '7d' }),
    }));
    await settle(fixture);
    page.pickRange('today');
    await settle(fixture);
    expect(requests).toContain('GET /api/v1/analytics/summary?range=today');
    expect(page.summary()?.range).toBe('today');
  });

  it('selecting a node shows its facts', async () => {
    const { fixture, page, el } = setup(() => ({ status: 200, body: summary() }));
    await settle(fixture);
    page.pick('memory');
    fixture.detectChanges();
    expect(el.querySelector('.facts')?.textContent).toContain('الذاكرة');
    expect(el.querySelector('.node[aria-pressed="true"]')?.textContent).toContain('الذاكرة');
  });

  it('shows empty states when there is no activity', async () => {
    const quiet: AnalyticsSummary = summary({
      queue: [], tools: [],
      trend: { step: 'day', starts: [], chat_turns: [0], tool_completed: [0], tool_failed: [0] },
    });
    const { fixture, el } = setup(() => ({ status: 200, body: quiet }));
    await settle(fixture);
    expect(el.querySelector('.queue .empty')?.textContent).toContain('لا توجد إجراءات بعد');
    expect(el.textContent).toContain('لا نشاط في هذه الفترة');
    expect(el.querySelector('app-line-chart')).toBeNull();
  });

  it('pause asks the server, then reloads', async () => {
    let paused = false;
    const { fixture, page, requests, el } = setup((method, path) => {
      if (method === 'POST') {
        paused = path.endsWith('/pause');
        return { status: 200, body: { paused } };
      }
      return { status: 200, body: summary({ paused }) };
    });
    await settle(fixture);
    await page.togglePause();
    fixture.detectChanges();
    expect(requests).toContain('POST /api/v1/control/pause');
    expect(el.querySelector('.pause')?.textContent).toContain('استئناف');
    expect(el.textContent).toContain('المساعد متوقف مؤقتاً');
  });

  it('shows the error when the server is down', async () => {
    const { fixture, el } = setup(() => {
      throw 'The assistant server is not running on port 8000.';
    });
    await settle(fixture);
    expect(el.querySelector('[role="alert"]')?.textContent).toContain('not running');
  });

  it('follows the saved layout: hidden widgets are not drawn, order is kept', async () => {
    const { fixture, el } = setup(() => ({ status: 200, body: summary() }));
    const layout = TestBed.inject(LayoutService);
    layout.toggle('kpis');
    layout.move('tools', -1);
    layout.move('tools', -1);
    await settle(fixture);
    expect(el.querySelector('.kpi')).toBeNull();
    const order = [...el.querySelectorAll('.widget')].map((w) => w.getAttribute('data-widget'));
    expect(order).toEqual(['map', 'tools', 'queue', 'trend', 'learning']);
  });

  it('says so when every widget is hidden, with a link to customize', async () => {
    const { fixture, el } = setup(() => ({ status: 200, body: summary() }));
    const layout = TestBed.inject(LayoutService);
    for (const id of ['kpis', 'map', 'queue', 'trend', 'tools', 'learning'] as const) layout.toggle(id);
    await settle(fixture);
    expect(el.querySelector('.empty-all')?.textContent).toContain('كل الأقسام مخفية');
  });
});
