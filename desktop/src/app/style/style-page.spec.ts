import { TestBed } from '@angular/core/testing';

import { Invoke, TAURI_INVOKE } from '../core/api';
import { StylePage, TEST_PROMPT } from './style-page';

function setup(pairs = 12) {
  const calls: { key: string; body: unknown }[] = [];
  let profile = { enabled: false, description: 'قصير', samples: ['مثال'], updated_at: null as string | null };
  const invoke: Invoke = async <T>(_c: string, args?: Record<string, unknown>) => {
    const key = `${args?.['method']} ${args?.['path']}`;
    calls.push({ key, body: args?.['body'] });
    const ok = (body: unknown) => ({ status: 200, body }) as T;
    if (key === 'GET /api/v1/style') return ok({ ...profile, samples: [...profile.samples] });
    if (key === 'PUT /api/v1/style') {
      profile = { ...(args?.['body'] as typeof profile), updated_at: 'now' };
      return ok({ ...profile, samples: [...profile.samples] });
    }
    if (key === 'GET /api/v1/style/dataset') return ok({ pairs, min_needed: 300, ready: pairs >= 300 });
    if (key === 'POST /api/v1/style/dataset/export') return ok({ path: '/data/training/style-1.jsonl', pairs });
    if (key === 'POST /api/v1/chat') return ok({ model: 'm', answer: 'آسف على التأخير' });
    throw new Error(key);
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  const fixture = TestBed.createComponent(StylePage);
  return { fixture, calls, el: fixture.nativeElement as HTMLElement };
}

const settle = async (f: { whenStable(): Promise<unknown>; detectChanges(): void }) => {
  await f.whenStable();
  f.detectChanges();
};

describe('StylePage', () => {
  it('loads the profile and dataset progress', async () => {
    const { fixture, el } = setup(12);
    await settle(fixture);
    expect((el.querySelector('#style-description') as HTMLTextAreaElement).value).toBe('قصير');
    expect(el.querySelectorAll('.sample textarea').length).toBe(1);
    expect(el.textContent).toContain('12 / 300');
    expect(el.textContent).toContain('قليل جدًا');
  });

  it('adds up to 10 samples, removes, and saves', async () => {
    const { fixture, calls, el } = setup();
    await settle(fixture);
    const add = el.querySelector('.add') as HTMLButtonElement;
    for (let i = 0; i < 12; i++) { add.click(); fixture.detectChanges(); }
    expect(el.querySelectorAll('.sample textarea').length).toBe(10);
    (el.querySelector('.remove') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(el.querySelectorAll('.sample textarea').length).toBe(9);
    (el.querySelector('#style-enabled') as HTMLInputElement).click();
    (el.querySelector('.save') as HTMLButtonElement).click();
    await settle(fixture);
    const put = calls.find((c) => c.key === 'PUT /api/v1/style')!.body as { enabled: boolean; samples: string[] };
    expect(put.enabled).toBe(true);
    expect(put.samples).toEqual([]); // blank samples are dropped
    expect(el.textContent).toContain('تم حفظ');
  });

  it('tests the style through chat and shows the reply', async () => {
    const { fixture, calls, el } = setup();
    await settle(fixture);
    (el.querySelector('.test') as HTMLButtonElement).click();
    await settle(fixture);
    expect(calls.find((c) => c.key === 'POST /api/v1/chat')!.body).toEqual({ message: TEST_PROMPT });
    expect(el.querySelector('.reply')?.textContent).toContain('آسف على التأخير');
  });

  it('exports even below 300 and shows the path', async () => {
    const { fixture, el } = setup(5);
    await settle(fixture);
    (el.querySelector('.export') as HTMLButtonElement).click();
    await settle(fixture);
    expect(el.querySelector('.path')?.textContent).toContain('style-1.jsonl');
  });
});
