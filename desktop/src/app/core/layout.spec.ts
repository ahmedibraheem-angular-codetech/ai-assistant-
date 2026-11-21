import { TestBed } from '@angular/core/testing';

import { LayoutService, WIDGETS, defaultLayout, normalize } from './layout';

const KEY = 'assistant.home-layout.v1';

describe('layout', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('reads a stored layout defensively', () => {
    expect(normalize(null)).toEqual(defaultLayout());
    expect(normalize('nonsense')).toEqual(defaultLayout());
    const read = normalize({ order: ['tools', 'ghost', 'tools', 'map'], hidden: ['queue', 'ghost'], density: 'tiny', motion: false });
    expect(read.order.slice(0, 2)).toEqual(['tools', 'map']); // unknown and duplicate dropped
    expect(read.order).toHaveLength(WIDGETS.length); // widgets added later are appended
    expect(read.hidden).toEqual(['queue']);
    expect(read.density).toBe('comfy');
    expect(read.motion).toBe(false);
  });

  it('toggles, moves within bounds, and remembers across restarts', () => {
    const first = TestBed.inject(LayoutService);
    first.toggle('queue');
    first.move('tools', -1);
    first.move('insights', -1); // already first: nothing happens
    first.setDensity('compact');
    expect(first.visible().map((w) => w.id)).toEqual(['insights', 'kpis', 'map', 'tools', 'trend', 'learning']);

    TestBed.resetTestingModule();
    const again = TestBed.inject(LayoutService);
    expect(again.layout().hidden).toEqual(['queue']);
    expect(again.layout().density).toBe('compact');
    expect(again.visible()[3].id).toBe('tools');

    again.reset();
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(defaultLayout());
  });
});
