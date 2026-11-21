import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from './api';
import { DISMISSED_KEY, Insight, InsightsStore, cleanInsights, detailHash, readDismissed } from './insights';

const item = (over: Partial<Insight> = {}): Insight => ({
  id: 'approvals-waiting', kind: 'approvals', severity: 'warn', title: 't', detail: '2 طلب', link: '/approvals', ...over,
});

function store(items: unknown) {
  const invoke: Invoke = async <T>(command: string) => {
    if (command === 'api_health') return true as T;
    return { status: 200, body: structuredClone(items) } as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  return TestBed.inject(InsightsStore);
}

describe('insights', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('reads dismissed ids defensively', () => {
    expect(readDismissed()).toEqual({});
    localStorage.setItem(DISMISSED_KEY, '{broken');
    expect(readDismissed()).toEqual({});
    localStorage.setItem(DISMISSED_KEY, '[1]');
    expect(readDismissed()).toEqual({});
    localStorage.setItem(DISMISSED_KEY, JSON.stringify({ a: 'x', b: 5 }));
    expect(readDismissed()).toEqual({ a: 'x' });
  });

  it('keeps only well-formed insights', () => {
    expect(cleanInsights(null)).toEqual([]);
    expect(cleanInsights({ insights: 'no' })).toEqual([]);
    expect(cleanInsights({ insights: [item(), { id: 1 }, null, item({ severity: 'odd' as never })] })).toHaveLength(1);
  });

  it('dismisses an insight, remembers it, and shows it again when the detail changes', async () => {
    const first = store({ insights: [item(), item({ id: 'lessons-review', severity: 'info' })], count: 2 });
    await first.refresh();
    expect(first.visible()).toHaveLength(2);
    expect(first.urgent()).toBe(1);

    first.dismiss(first.all()[0]);
    expect(first.visible().map((i) => i.id)).toEqual(['lessons-review']);
    expect(first.urgent()).toBe(0);
    expect(JSON.parse(localStorage.getItem(DISMISSED_KEY)!)['approvals-waiting']).toBe(detailHash('2 طلب'));

    TestBed.resetTestingModule();
    const again = store({ insights: [item()], count: 1 });
    await again.refresh();
    expect(again.visible()).toHaveLength(0);

    TestBed.resetTestingModule();
    const changed = store({ insights: [item({ detail: '5 طلب' })], count: 1 });
    await changed.refresh();
    expect(changed.visible()).toHaveLength(1);
  });
});
