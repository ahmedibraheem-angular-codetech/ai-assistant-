import { Injectable, computed, inject, signal } from '@angular/core';

import { AssistantApi } from './api';

export type InsightSeverity = 'info' | 'warn' | 'high';

export interface Insight {
  id: string;
  kind: string;
  severity: InsightSeverity;
  title: string;
  detail: string;
  link: string;
}

export const DISMISSED_KEY = 'assistant.insights.dismissed.v1';
const EVERY_MS = 60_000;

/** Small stable hash, so a changed detail brings a dismissed insight back. */
export function detailHash(text: string): string {
  let hash = 5381;
  for (let i = 0; i < text.length; i++) hash = ((hash << 5) + hash + text.charCodeAt(i)) | 0;
  return (hash >>> 0).toString(36);
}

/** Dismissed id -> detail hash. Anything unreadable counts as nothing dismissed. */
export function readDismissed(): Record<string, string> {
  try {
    const raw = JSON.parse(localStorage.getItem(DISMISSED_KEY) ?? 'null');
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
    return Object.fromEntries(Object.entries(raw).filter(([, value]) => typeof value === 'string')) as Record<string, string>;
  } catch {
    return {};
  }
}

function writeDismissed(value: Record<string, string>): void {
  try {
    localStorage.setItem(DISMISSED_KEY, JSON.stringify(value));
  } catch {
    // storage unavailable: the dismissal lasts until the app closes
  }
}

export function isDismissed(item: Insight, dismissed: Record<string, string>): boolean {
  return dismissed[item.id] === detailHash(item.detail);
}

/** Keeps only well-formed insights from a server answer. */
export function cleanInsights(body: unknown): Insight[] {
  const list = (body as { insights?: unknown } | null)?.insights;
  if (!Array.isArray(list)) return [];
  return list.filter(
    (item): item is Insight =>
      !!item && typeof item.id === 'string' && typeof item.title === 'string' && typeof item.detail === 'string'
      && typeof item.link === 'string' && ['info', 'warn', 'high'].includes(item.severity),
  );
}

/** Proactive insights, shared by the home widget and the side menu badge. Polls quietly. */
@Injectable({ providedIn: 'root' })
export class InsightsStore {
  private readonly api = inject(AssistantApi);
  private timer: ReturnType<typeof setInterval> | null = null;

  readonly all = signal<Insight[]>([]);
  readonly dismissed = signal<Record<string, string>>(readDismissed());
  readonly loaded = signal(false);

  readonly visible = computed(() => this.all().filter((item) => !isDismissed(item, this.dismissed())));
  /** Undismissed warn/high insights: the side menu badge. */
  readonly urgent = computed(() => this.visible().filter((item) => item.severity !== 'info').length);

  start(): void {
    if (this.timer) return;
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), EVERY_MS);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async refresh(): Promise<void> {
    try {
      this.all.set(cleanInsights(await this.api.request('GET', '/api/v1/insights')));
    } catch {
      // keep the last list
    } finally {
      this.loaded.set(true);
    }
  }

  dismiss(item: Insight): void {
    const next = { ...this.dismissed(), [item.id]: detailHash(item.detail) };
    this.dismissed.set(next);
    writeDismissed(next);
  }
}
