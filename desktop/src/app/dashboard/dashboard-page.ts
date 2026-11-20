import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssistantApi } from '../core/api';
import { LayoutService } from '../core/layout';
import { AnalyticsRange, AnalyticsSummary } from '../core/models';
import { LineChart } from '../shared/line-chart';
import {
  RANGES,
  hasTrendData,
  kpiCards,
  mapNodes,
  queueRows,
  toolBars,
  trendLabels,
  trendSeries,
  fmt,
} from './dashboard-view';

const REFRESH_MS = 30_000;
const RING = 2 * Math.PI * 26;

@Component({
  selector: 'app-dashboard-page',
  imports: [LineChart, RouterLink],
  templateUrl: './dashboard-page.html',
  styleUrl: './dashboard-page.css',
})
export class DashboardPage {
  private readonly api = inject(AssistantApi);
  readonly layout = inject(LayoutService);

  readonly ranges = RANGES;
  readonly range = signal<AnalyticsRange>('7d');
  readonly summary = signal<AnalyticsSummary | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly busy = signal(false);
  readonly selected = signal<string>('approvals');
  readonly fmt = fmt;

  readonly kpis = computed(() => {
    const s = this.summary();
    return s ? kpiCards(s).map((k) => ({ ...k, dash: `${(k.ring * RING).toFixed(1)} ${RING.toFixed(1)}` })) : [];
  });
  readonly map = computed(() => {
    const s = this.summary();
    return s ? mapNodes(s) : { nodes: [], hiddenAgents: 0 };
  });
  readonly selectedNode = computed(() => {
    const nodes = this.map().nodes;
    return nodes.find((node) => node.id === this.selected()) ?? (nodes.length ? nodes[0] : null);
  });
  readonly queue = computed(() => queueRows(this.summary()?.queue ?? []));
  readonly tools = computed(() => {
    const s = this.summary();
    return s ? toolBars(s) : [];
  });
  readonly hasTrend = computed(() => {
    const s = this.summary();
    return !!s && hasTrendData(s);
  });
  readonly trendLabels = computed(() => {
    const s = this.summary();
    return s ? trendLabels(s) : [];
  });
  readonly trend = computed(() => {
    const s = this.summary();
    return s ? trendSeries(s) : [];
  });

  constructor() {
    void this.load();
    const timer = setInterval(() => void this.load(true), REFRESH_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
  }

  pickRange(range: AnalyticsRange): void {
    if (range === this.range()) return;
    this.range.set(range);
    void this.load();
  }

  pick(id: string): void {
    this.selected.set(id);
  }

  async load(quiet = false): Promise<void> {
    const range = this.range();
    if (!quiet) this.loading.set(true);
    try {
      const summary = await this.api.analytics(range);
      if (range !== this.range()) return; // the user switched range meanwhile
      this.summary.set(summary);
      this.error.set('');
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.loading.set(false);
    }
  }

  async togglePause(): Promise<void> {
    const paused = this.summary()?.paused ?? false;
    this.busy.set(true);
    try {
      await this.api.request('POST', paused ? '/api/v1/control/resume' : '/api/v1/control/pause', paused ? null : { reason: 'من لوحة التحليلات' });
      await this.load(true);
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(false);
    }
  }
}
