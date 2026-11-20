import { Component, computed, input, signal } from '@angular/core';

export interface LineSeries {
  name: string;
  /** A CSS colour or token, e.g. `var(--accent)`. */
  color: string;
  data: number[];
  area?: boolean;
  dashed?: boolean;
}

const W = 600;
const H = 180;
const TOP = 8;

/**
 * Small SVG line chart: any number of series, hover/focus tooltip, and a
 * hidden data table for screen readers. Plain SVG on purpose: it works under
 * Tauri's frozen prototype and the strict CSP, with no chart library.
 */
@Component({
  selector: 'app-line-chart',
  template: `
    <div class="legend">
      @for (s of series(); track s.name) {
        <span><i [style.background]="s.color" [class.dashed]="s.dashed"></i>{{ s.name }}</span>
      }
    </div>
    <div class="plot" (mouseleave)="hover.set(null)">
      <div class="axis">
        @for (tick of ticks(); track $index) {
          <span [style.bottom.%]="tick.at">{{ tick.label }}</span>
        }
      </div>
      <svg [attr.viewBox]="'0 0 ' + width + ' ' + height" preserveAspectRatio="none" aria-hidden="true">
        @for (tick of ticks(); track $index) {
          <line x1="0" [attr.x2]="width" [attr.y1]="tick.y" [attr.y2]="tick.y" class="grid"></line>
        }
        @for (p of paths(); track p.name) {
          @if (p.area) {
            <path [attr.d]="p.area" [style.fill]="p.color" class="area"></path>
          }
          <path [attr.d]="p.line" [style.stroke]="p.color" [class.dashed]="p.dashed" class="line"></path>
        }
        @if (hover() !== null) {
          <line [attr.x1]="x(hover()!)" [attr.x2]="x(hover()!)" y1="0" [attr.y2]="height" class="cursor"></line>
        }
        @for (label of labels(); track $index) {
          <rect [attr.x]="x($index) - step() / 2" y="0" [attr.width]="step()" [attr.height]="height" class="hit"
            (mouseenter)="hover.set($index)"></rect>
        }
      </svg>
      @if (tip(); as t) {
        <div class="tip" [style.left.%]="t.left" role="status">
          <strong>{{ t.label }}</strong>
          @for (row of t.rows; track row.name) {
            <span><i [style.background]="row.color"></i>{{ row.name }}: {{ row.value }}</span>
          }
        </div>
      }
    </div>
    <div class="labels" aria-hidden="true">
      @for (label of edgeLabels(); track $index) {
        <span>{{ label }}</span>
      }
    </div>
    <table class="visually-hidden">
      <caption>{{ caption() }}</caption>
      <tr><th>الفترة</th>@for (s of series(); track s.name) {<th>{{ s.name }}</th>}</tr>
      @for (label of labels(); track $index; let i = $index) {
        <tr><td>{{ label }}</td>@for (s of series(); track s.name) {<td>{{ s.data[i] }}</td>}</tr>
      }
    </table>
  `,
  styles: `
    :host { display: flex; flex-direction: column; gap: 6px; font-size: 12px; color: var(--muted); }
    .legend { display: flex; gap: 14px; flex-wrap: wrap; }
    .legend i, .tip i { display: inline-block; width: 14px; height: 3px; border-radius: 2px; margin-inline-end: 6px; vertical-align: middle; }
    .legend i.dashed { opacity: 0.7; }
    .plot { position: relative; height: 160px; padding-left: 28px; direction: ltr; }
    .axis { position: absolute; left: 0; top: 4px; bottom: 0; width: 24px; }
    .axis span { position: absolute; left: 0; transform: translateY(50%); font-size: 11px; }
    svg { width: 100%; height: 100%; display: block; overflow: visible; }
    .grid { stroke: var(--border); vector-effect: non-scaling-stroke; }
    .line { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round; }
    .line.dashed { stroke-dasharray: 5 5; }
    .area { opacity: 0.12; }
    .cursor { stroke: var(--muted); stroke-opacity: 0.5; vector-effect: non-scaling-stroke; }
    .hit { fill: transparent; }
    .tip { position: absolute; top: 0; transform: translateX(-50%); pointer-events: none; display: flex; flex-direction: column; gap: 2px;
      padding: 8px 10px; border-radius: 10px; background: var(--surface); border: 1px solid var(--border); color: var(--text); white-space: nowrap;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25); }
    .labels { display: flex; justify-content: space-between; padding-left: 28px; direction: ltr; }
    .tip { direction: rtl; }
  `,
})
export class LineChart {
  readonly labels = input.required<string[]>();
  readonly series = input.required<LineSeries[]>();
  readonly caption = input('');

  readonly width = W;
  readonly height = H;
  readonly hover = signal<number | null>(null);

  private readonly max = computed(() => {
    const peak = Math.max(0, ...this.series().flatMap((s) => s.data));
    if (peak <= 4) return 4;
    const magnitude = 10 ** Math.floor(Math.log10(peak));
    return Math.ceil(peak / magnitude) * magnitude;
  });

  readonly step = computed(() => W / Math.max(1, this.labels().length - 1));

  x(index: number): number {
    return this.labels().length <= 1 ? W / 2 : index * this.step();
  }

  private y(value: number): number {
    return H - (value / this.max()) * (H - TOP);
  }

  readonly ticks = computed(() =>
    [0, 0.5, 1].map((part) => {
      const value = Math.round(this.max() * part);
      return { label: String(value), y: this.y(value), at: part * ((H - TOP) / H) * 100 };
    }),
  );

  readonly paths = computed(() =>
    this.series().map((s) => {
      const points = s.data.map((value, i) => `${this.x(i).toFixed(1)} ${this.y(value).toFixed(1)}`);
      const line = points.length ? `M${points.join(' L')}` : '';
      const area = s.area && points.length ? `${line} L${this.x(s.data.length - 1).toFixed(1)} ${H} L${this.x(0).toFixed(1)} ${H} Z` : '';
      return { name: s.name, color: s.color, dashed: !!s.dashed, line, area };
    }),
  );

  /** First, middle and last labels under the plot (left to right in time). */
  readonly edgeLabels = computed(() => {
    const all = this.labels();
    if (all.length <= 3) return all;
    return [all[0], all[Math.floor(all.length / 2)], all[all.length - 1]];
  });

  readonly tip = computed(() => {
    const index = this.hover();
    if (index === null) return null;
    return {
      label: this.labels()[index],
      left: (this.x(index) / W) * 100,
      rows: this.series().map((s) => ({ name: s.name, color: s.color, value: s.data[index] ?? 0 })),
    };
  });
}
