import { TestBed } from '@angular/core/testing';

import { LineChart } from './line-chart';

function render(data: number[][], labels = ['1/11', '2/11', '3/11', '4/11']) {
  const fixture = TestBed.createComponent(LineChart);
  fixture.componentRef.setInput('labels', labels);
  fixture.componentRef.setInput('series', data.map((values, i) => ({ name: `s${i}`, color: 'red', data: values, area: i === 0 })));
  fixture.componentRef.setInput('caption', 'نشاط');
  fixture.detectChanges();
  return { fixture, chart: fixture.componentInstance, el: fixture.nativeElement as HTMLElement };
}

describe('LineChart', () => {
  it('draws one line per series, an area for the first, and a rounded scale', () => {
    const { el, chart } = render([[1, 7, 3, 12], [0, 1, 0, 2]]);
    expect(el.querySelectorAll('path.line')).toHaveLength(2);
    expect(el.querySelectorAll('path.area')).toHaveLength(1);
    expect(chart.ticks().map((t) => t.label)).toEqual(['0', '10', '20']);
    expect(el.querySelector('path.line')?.getAttribute('d')).toMatch(/^M0\.0 /);
  });

  it('shows every value of a point on hover', () => {
    const { fixture, el } = render([[1, 7, 3, 12], [0, 1, 0, 2]]);
    el.querySelectorAll('rect.hit')[3].dispatchEvent(new Event('mouseenter'));
    fixture.detectChanges();
    const tip = el.querySelector('.tip')?.textContent ?? '';
    expect(tip).toContain('4/11');
    expect(tip).toContain('s0: 12');
    expect(tip).toContain('s1: 2');
  });

  it('gives screen readers the data as a table', () => {
    const { el } = render([[1, 2, 3, 4]]);
    expect(el.querySelector('caption')?.textContent).toBe('نشاط');
    expect(el.querySelectorAll('table tr')).toHaveLength(5);
  });

  it('copes with a single point and all zeros', () => {
    const { el, chart } = render([[0]], ['اليوم']);
    expect(chart.ticks().map((t) => t.label)).toEqual(['0', '2', '4']);
    expect(el.querySelector('path.line')?.getAttribute('d')).toBe('M300.0 180.0');
  });
});
