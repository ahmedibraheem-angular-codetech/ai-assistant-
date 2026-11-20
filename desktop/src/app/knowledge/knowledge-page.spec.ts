import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { GraphEntity } from '../core/models';
import { KnowledgePage } from './knowledge-page';

function setup() {
  const entities: GraphEntity[] = [
    { id: 'e1', name: 'أحمد', type: 'person', description: 'المالك', author: 'user', agent_id: null, updated_at: '' },
    { id: 'e2', name: 'الشركة', type: 'organization', description: '', author: 'agent', agent_id: 'a1', updated_at: '' },
  ];
  const requests: { method: string; path: string; body: unknown }[] = [];
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const method = String(args?.['method']);
    const path = String(args?.['path']);
    requests.push({ method, path, body: args?.['body'] });
    if (path === '/api/v1/graph/entities') return { status: 200, body: { entities: entities.map((e) => ({ ...e })), count: entities.length } } as T;
    if (path.startsWith('/api/v1/graph/lookup')) {
      return { status: 200, body: { matches: [{ entity: entities[0], relations: [
        { id: 'r1', subject: 'أحمد', subject_type: 'person', predicate: 'يعمل في', object: 'الشركة', object_type: 'organization', note: '', author: 'user' },
      ] }] } } as T;
    }
    if (path === '/api/v1/graph/relations') return { status: 201, body: {} } as T;
    if (method === 'DELETE') return { status: 204, body: null } as T;
    return { status: 404, body: { detail: 'not found' } } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(KnowledgePage);
  return { fixture, page: fixture.componentInstance, requests, el: fixture.nativeElement as HTMLElement };
}

async function settle(fixture: { whenStable: () => Promise<unknown>; detectChanges: () => void }) {
  await fixture.whenStable();
  fixture.detectChanges();
}

describe('KnowledgePage', () => {
  it('groups entities and marks those written by agents', async () => {
    const { fixture, el } = setup();
    await settle(fixture);
    expect([...el.querySelectorAll('.list h2')].map((h) => h.textContent?.trim())).toEqual(['أشخاص 1', 'جهات 1']);
    expect(el.querySelectorAll('.entity')[1].textContent).toContain('وكيل');
  });

  it('opening an entity draws its neighbours and lists relations', async () => {
    const { fixture, page, el, requests } = setup();
    await settle(fixture);
    await page.open(page.entities()[0]);
    fixture.detectChanges();
    expect(requests.some((r) => r.path === '/api/v1/graph/lookup?name=%D8%A3%D8%AD%D9%85%D8%AF')).toBe(true);
    expect(el.querySelector('.graph .center')?.textContent).toContain('أحمد');
    expect(el.querySelectorAll('.graph .node')).toHaveLength(1);
    expect(el.querySelector('.relation')?.textContent).toContain('يعمل في');
  });

  it('adds a relation with all three parts, and refuses missing parts', async () => {
    const { fixture, page, requests } = setup();
    await settle(fixture);
    page.subject.set('أحمد');
    page.predicate.set('');
    await page.addRelation();
    expect(page.error()).toContain('الطرفين');
    page.predicate.set('يدير');
    page.object.set('المشروع');
    await page.addRelation();
    expect(requests).toContainEqual({ method: 'POST', path: '/api/v1/graph/relations', body: { subject: 'أحمد', relation: 'يدير', object: 'المشروع' } });
  });

  it('search filters the list', async () => {
    const { fixture, page, el } = setup();
    await settle(fixture);
    page.search.set('المالك');
    fixture.detectChanges();
    expect(el.querySelectorAll('.entity')).toHaveLength(1);
  });
});
