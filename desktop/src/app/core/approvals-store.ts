import { Injectable, inject, signal } from '@angular/core';

import { AssistantApi } from './api';
import { Approval } from './models';

const EVERY_MS = 15_000;

/**
 * Pending approvals, shared by the side menu badge and the approvals page.
 * Polls quietly; a failed poll keeps the last list.
 */
@Injectable({ providedIn: 'root' })
export class ApprovalsStore {
  private readonly api = inject(AssistantApi);
  private timer: ReturnType<typeof setInterval> | null = null;

  readonly pending = signal<Approval[]>([]);
  readonly loaded = signal(false);
  readonly error = signal('');

  /** Starts polling once; safe to call from several places. */
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
      const list = await this.api.pendingApprovals();
      this.pending.set(list.approvals);
      this.error.set('');
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.loaded.set(true);
    }
  }
}
