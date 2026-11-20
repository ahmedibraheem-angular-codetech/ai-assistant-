import { Component, computed, inject, signal } from '@angular/core';

import { AssistantApi } from '../core/api';
import { ApprovalsStore } from '../core/approvals-store';
import { Approval } from '../core/models';
import { approvable, changeTitle, hiddenCount, outputText, risk, status, timeLeft, when } from './approvals-view';

type Tab = 'pending' | 'history';
const HISTORY_SHOWN = 50;

@Component({
  selector: 'app-approvals-page',
  templateUrl: './approvals-page.html',
  styleUrl: './approvals-page.css',
})
export class ApprovalsPage {
  private readonly api = inject(AssistantApi);
  readonly store = inject(ApprovalsStore);

  readonly tab = signal<Tab>('pending');
  readonly history = signal<Approval[]>([]);
  readonly agentNames = signal<Record<string, string>>({});
  readonly selectedId = signal<string | null>(null);
  readonly confirming = signal(false);
  readonly deciding = signal(false);
  readonly notice = signal<{ tone: 'ok' | 'danger' | 'muted'; text: string; detail?: string } | null>(null);
  readonly error = signal('');

  readonly list = computed(() => (this.tab() === 'pending' ? this.store.pending() : this.history().slice(0, HISTORY_SHOWN)));
  readonly selected = computed(() => {
    const items = this.list();
    return items.find((item) => item.id === this.selectedId()) ?? (items.length ? items[0] : null);
  });
  readonly check = computed(() => {
    const item = this.selected();
    return item ? approvable(item) : { ok: false, why: '' };
  });

  readonly risk = risk;
  readonly status = status;
  readonly timeLeft = timeLeft;
  readonly when = when;
  readonly changeTitle = changeTitle;
  readonly hiddenCount = hiddenCount;

  constructor() {
    this.store.start();
    void this.store.refresh();
    void this.loadAgents();
  }

  agent(a: Approval): string {
    return (a.agent_id && this.agentNames()[a.agent_id]) || 'المساعد';
  }

  showTab(tab: Tab): void {
    this.tab.set(tab);
    this.selectedId.set(null);
    this.confirming.set(false);
    if (tab === 'history') void this.loadHistory();
  }

  select(id: string): void {
    this.selectedId.set(id);
    this.confirming.set(false);
  }

  askConfirm(): void {
    if (this.check().ok) this.confirming.set(true);
  }

  async approve(): Promise<void> {
    const item = this.selected();
    if (!item || !this.check().ok || this.deciding()) return;
    await this.decide(item, async () => {
      const result = await this.api.approve(item.id);
      const out = outputText(result.output) || result.result || '';
      const s = status(result);
      this.notice.set({
        tone: result.status === 'COMPLETED' ? 'ok' : result.status === 'FAILED' ? 'danger' : 'muted',
        text: `${item.tool_name}: ${s.label}`,
        detail: out,
      });
    });
  }

  async reject(): Promise<void> {
    const item = this.selected();
    if (!item || this.deciding()) return;
    await this.decide(item, async () => {
      await this.api.reject(item.id);
      this.notice.set({ tone: 'muted', text: `رفضت ${item.tool_name}. لن يُنفذ شيء.` });
    });
  }

  private async decide(item: Approval, action: () => Promise<void>): Promise<void> {
    this.deciding.set(true);
    this.error.set('');
    this.notice.set(null);
    try {
      await action();
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.deciding.set(false);
      this.confirming.set(false);
      if (this.selectedId() === item.id) this.selectedId.set(null);
      await this.store.refresh();
    }
  }

  private async loadHistory(): Promise<void> {
    try {
      this.history.set((await this.api.approvalHistory()).approvals);
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    }
  }

  private async loadAgents(): Promise<void> {
    try {
      const { agents } = await this.api.agents();
      this.agentNames.set(Object.fromEntries(agents.map((a) => [a.id, a.name])));
    } catch {
      // Names are a nicety; the list still works with "المساعد".
    }
  }
}
