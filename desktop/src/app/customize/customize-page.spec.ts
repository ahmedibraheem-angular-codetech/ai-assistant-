import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';

import { LayoutService } from '../core/layout';
import { CustomizePage } from './customize-page';

function setup() {
  TestBed.configureTestingModule({ providers: [provideRouter([])] });
  const fixture = TestBed.createComponent(CustomizePage);
  fixture.detectChanges();
  return { fixture, el: fixture.nativeElement as HTMLElement, layout: TestBed.inject(LayoutService) };
}

describe('CustomizePage', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('lists every widget with a switch and shows the count', () => {
    const { el } = setup();
    expect(el.querySelectorAll('.row:not(.plain)')).toHaveLength(6);
    expect(el.querySelector('.card-head')?.textContent).toContain('6 من 6');
    expect(el.querySelectorAll('.preview .tile')).toHaveLength(6);
  });

  it('switching a widget off hides it from the preview and the home page layout', () => {
    const { fixture, el, layout } = setup();
    el.querySelector<HTMLInputElement>('#w-queue')?.click();
    fixture.detectChanges();
    expect(layout.isVisible('queue')).toBe(false);
    expect(el.querySelectorAll('.preview .tile')).toHaveLength(5);
    expect(el.querySelector('.card-head')?.textContent).toContain('5 من 6');
  });

  it('moves a widget up, and the first one cannot move up', () => {
    const { fixture, el, layout } = setup();
    const up = el.querySelectorAll<HTMLButtonElement>('.row:not(.plain) .icon');
    expect(up[0].disabled).toBe(true); // first row's up button
    el.querySelector<HTMLButtonElement>('[aria-label="حرّك خريطة المساعد لأعلى"]')?.click();
    fixture.detectChanges();
    expect(layout.layout().order.slice(0, 2)).toEqual(['map', 'kpis']);
  });

  it('density and motion apply at once; reset restores everything', () => {
    const { fixture, el, layout } = setup();
    el.querySelectorAll<HTMLButtonElement>('.segmented button')[1].click();
    el.querySelector<HTMLInputElement>('#motion')?.click();
    fixture.detectChanges();
    expect(layout.layout().density).toBe('compact');
    expect(layout.layout().motion).toBe(false);
    expect(el.querySelector('.preview')?.classList).toContain('compact');
    el.querySelector<HTMLButtonElement>('.top button')?.click();
    expect(layout.layout()).toEqual({ order: ['kpis', 'map', 'queue', 'trend', 'tools', 'learning'], hidden: [], density: 'comfy', motion: true });
  });
});
