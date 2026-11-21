import { Component, computed, inject, signal } from '@angular/core';

import { AssistantApi } from '../core/api';
import { N8nSection } from './n8n-section';
import { Agent, AgentBudget, StandingApproval } from '../core/models';
import { VoiceService } from '../core/voice';
import {
  hasLimits, standingExpiry, standingScope, usagePercent, usageText, usageTone,
  validateBudget, validateStanding,
} from './settings-view';

type Section = 'control' | 'budgets' | 'standing';
const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

@Component({
  selector: 'app-settings-page',
  imports: [N8nSection],
  templateUrl: './settings-page.html',
  styleUrl: './settings-page.css',
})
export class SettingsPage {
  private readonly api = inject(AssistantApi);
  readonly voice = inject(VoiceService);

  readonly paused = signal<boolean | null>(null);
  readonly agents = signal<Agent[]>([]);
  readonly budgets = signal<Record<string, AgentBudget>>({});
  readonly standing = signal<StandingApproval[]>([]);
  readonly loading = signal<Record<Section, boolean>>({ control: true, budgets: true, standing: true });
  readonly errors = signal<Record<Section, string>>({ control: '', budgets: '', standing: '' });
  readonly busy = signal('');

  // Budget edit form (one agent at a time).
  readonly editing = signal<string | null>(null);
  readonly toolsText = signal('');
  readonly chatsText = signal('');
  // Standing approval form.
  readonly saAgent = signal('');
  readonly saTool = signal('');
  readonly saPath = signal('');
  readonly saDays = signal('');

  readonly names = computed(() => Object.fromEntries(this.agents().map((a) => [a.id, a.name])));

  readonly usageText = usageText;
  readonly usagePercent = usagePercent;
  readonly usageTone = usageTone;
  readonly hasLimits = hasLimits;
  readonly standingScope = standingScope;
  readonly standingExpiry = standingExpiry;

  constructor() {
    void this.loadControl();
    void this.loadBudgets();
    void this.loadStanding();
    void this.voice.loadStatus();
  }

  pickVoice(name: string): void {
    this.voice.setVoice(name);
  }

  testVoice(): void {
    void this.voice.speak('مرحباً، أنا مساعدك الشخصي. هذا اختبار للصوت.');
  }

  name(id: string): string {
    return this.names()[id] || 'وكيل محذوف';
  }

  private fail(section: Section, error: unknown): void {
    this.errors.update((e) => ({ ...e, [section]: message(error) }));
  }
  private clear(section: Section): void {
    this.errors.update((e) => ({ ...e, [section]: '' }));
  }
  private done(section: Section): void {
    this.loading.update((l) => ({ ...l, [section]: false }));
  }

  private async loadControl(): Promise<void> {
    try {
      this.paused.set((await this.api.control()).paused);
    } catch (error) {
      this.fail('control', error);
    } finally {
      this.done('control');
    }
  }

  async togglePause(): Promise<void> {
    if (this.busy() || this.paused() === null) return;
    this.busy.set('control');
    this.clear('control');
    try {
      const state = this.paused() ? await this.api.resume() : await this.api.pause('من الإعدادات');
      this.paused.set(state.paused);
    } catch (error) {
      this.fail('control', error);
    } finally {
      this.busy.set('');
    }
  }

  private async loadBudgets(): Promise<void> {
    try {
      const { agents } = await this.api.agents();
      this.agents.set(agents);
      const results = await Promise.all(agents.map((a) => this.api.budget(a.id)));
      this.budgets.set(Object.fromEntries(results.map((b) => [b.agent_id, b])));
    } catch (error) {
      this.fail('budgets', error);
    } finally {
      this.done('budgets');
    }
  }

  edit(agentId: string): void {
    const b = this.budgets()[agentId];
    this.editing.set(agentId);
    this.toolsText.set(b?.daily_tool_calls?.toString() ?? '');
    this.chatsText.set(b?.daily_chat_turns?.toString() ?? '');
    this.clear('budgets');
  }

  cancelEdit(): void {
    this.editing.set(null);
    this.clear('budgets');
  }

  async saveBudget(): Promise<void> {
    const id = this.editing();
    if (!id || this.busy()) return;
    const check = validateBudget(this.toolsText(), this.chatsText());
    if (!check.ok) return this.fail('budgets', check.error);
    await this.changeBudget(id, () => this.api.setBudget(id, check.value));
  }

  async removeLimits(agentId: string): Promise<void> {
    if (this.busy()) return;
    await this.changeBudget(agentId, async () => {
      await this.api.clearBudget(agentId);
      return this.api.budget(agentId);
    });
  }

  private async changeBudget(agentId: string, action: () => Promise<AgentBudget>): Promise<void> {
    this.busy.set('budget:' + agentId);
    this.clear('budgets');
    try {
      const b = await action();
      this.budgets.update((all) => ({ ...all, [agentId]: b }));
      this.editing.set(null);
    } catch (error) {
      this.fail('budgets', error);
    } finally {
      this.busy.set('');
    }
  }

  private async loadStanding(): Promise<void> {
    try {
      this.standing.set((await this.api.standingApprovals()).standing_approvals);
    } catch (error) {
      this.fail('standing', error);
    } finally {
      this.done('standing');
    }
  }

  async addStanding(): Promise<void> {
    if (this.busy()) return;
    const check = validateStanding({ agentId: this.saAgent(), tool: this.saTool(), path: this.saPath(), days: this.saDays() });
    if (!check.ok) return this.fail('standing', check.error);
    this.busy.set('standing');
    this.clear('standing');
    try {
      await this.api.addStandingApproval(check.value);
      this.saTool.set('');
      this.saPath.set('');
      this.saDays.set('');
      this.standing.set((await this.api.standingApprovals()).standing_approvals);
    } catch (error) {
      this.fail('standing', error);
    } finally {
      this.busy.set('');
    }
  }

  async revoke(id: string): Promise<void> {
    if (this.busy()) return;
    this.busy.set('revoke:' + id);
    this.clear('standing');
    try {
      await this.api.revokeStandingApproval(id);
      this.standing.update((list) => list.filter((s) => s.id !== id));
    } catch (error) {
      this.fail('standing', error);
    } finally {
      this.busy.set('');
    }
  }

  value(event: Event): string {
    return (event.target as HTMLInputElement).value;
  }
}
