import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { Project, ProjectBrief } from '../core/models';
import { ProjectsPage } from './projects-page';

const project = (id: string, name: string): Project =>
  ({ id, name, description: 'وصف', archived: false, created_at: '', updated_at: '' });

function brief(p: Project): ProjectBrief {
  return {
    project: p,
    current_status: [{ id: 's1', text: 'المرحلة 5 جارية', author: 'agent', at: '2026-11-21T09:00:00Z' }],
    open_tasks: [{ id: 't1', text: 'صفحة المشاريع', author: 'user', at: '2026-11-21T09:00:00Z' }],
    recent_decisions: [{ id: 'd1', text: 'نبقى على SQLite', author: 'user', at: '2026-11-20T09:00:00Z' }],
    recent_notes: [],
    done_tasks: 4,
  };
}

function setup(projects = [project('p1', 'المساعد الشخصي'), project('p2', 'الموقع')]) {
  const requests: { method: string; path: string; body: unknown }[] = [];
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    const body = args?.['body'];
    requests.push({ method, path, body });
    if (path.startsWith('/api/v1/projects') && method === 'GET' && !path.includes('/brief')) {
      return { status: 200, body: { projects: projects.map((p) => ({ ...p })), count: projects.length } } as T;
    }
    if (path === '/api/v1/projects' && method === 'POST') {
      const created = project('p3', (body as { name: string }).name);
      projects.push(created);
      return { status: 201, body: created } as T;
    }
    const b = path.match(/projects\/(.+)\/brief$/);
    if (b) return { status: 200, body: brief(projects.find((p) => p.id === b[1])!) } as T;
    return { status: 200, body: {} } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(ProjectsPage);
  return { fixture, page: fixture.componentInstance, requests, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

describe('ProjectsPage', () => {
  it('opens the first project with status, open tasks and decisions', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    expect(el.querySelector('.main h2')?.textContent).toContain('المساعد الشخصي');
    expect(el.querySelector('.status')?.textContent).toContain('المرحلة 5 جارية');
    expect(el.querySelector('.status')?.textContent).toContain('من وكيل');
    expect(el.querySelector('.task')?.textContent).toContain('صفحة المشاريع');
    expect(el.textContent).toContain('4 منجزة');
    expect(el.textContent).toContain('نبقى على SQLite');
  });

  it('ticking a task marks it done on the server', async () => {
    const { fixture, page, requests } = setup();
    await settle(fixture);
    await page.setDone('t1', true);
    expect(requests).toContainEqual({ method: 'PATCH', path: '/api/v1/project-entries/t1', body: { done: true } });
  });

  it('adds an entry of the chosen kind', async () => {
    const { fixture, page, requests } = setup();
    await settle(fixture);
    page.kind.set('decision');
    page.text.set('نستخدم SVG للرسوم');
    await page.addEntry();
    expect(requests).toContainEqual({ method: 'POST', path: '/api/v1/projects/p1/entries', body: { kind: 'decision', text: 'نستخدم SVG للرسوم' } });
    expect(page.text()).toBe('');
  });

  it('creates a project and opens it', async () => {
    const { fixture, page } = setup();
    await settle(fixture);
    page.newName.set('بحث السوق');
    await page.create();
    await settle(fixture);
    expect(page.selectedId()).toBe('p3');
    expect(page.brief()?.project.name).toBe('بحث السوق');
  });

  it('shows an empty state with no projects', async () => {
    const { fixture, el } = setup([]);
    await settle(fixture);
    expect(el.querySelector('.empty')?.textContent).toContain('لا مشاريع بعد');
  });
});
