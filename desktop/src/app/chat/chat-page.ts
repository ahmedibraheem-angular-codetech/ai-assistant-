import {
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';

import { ApiError, AssistantApi } from '../core/api';
import { Conversation } from '../core/models';

export interface ChatLine {
  role: 'user' | 'assistant';
  content: string;
  meta?: string;
  pending?: boolean;
  failed?: boolean;
}

const HEALTH_EVERY_MS = 15_000;

@Component({
  selector: 'app-chat-page',
  imports: [FormsModule],
  templateUrl: './chat-page.html',
  styleUrl: './chat-page.css',
})
export class ChatPage {
  private readonly api = inject(AssistantApi);
  private readonly router = inject(Router);

  /** Conversation id from the route (`/chat/:id`); empty for a new chat. */
  readonly id = input<string | undefined>();

  readonly conversations = signal<Conversation[]>([]);
  readonly lines = signal<ChatLine[]>([]);
  readonly draft = signal('');
  readonly sending = signal(false);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly online = signal<boolean | null>(null);
  readonly canSend = computed(() => !this.sending() && this.draft().trim().length > 0);

  private readonly scroller = viewChild<ElementRef<HTMLElement>>('scroller');
  /** Set when we navigate to a conversation we just created, so we keep the lines on screen. */
  private justCreated: string | null = null;
  private loadToken = 0;

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => this.open(id));
    });
    effect(() => {
      this.lines();
      queueMicrotask(() => this.scrollToEnd());
    });
    const timer = setInterval(() => void this.checkHealth(), HEALTH_EVERY_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(timer));
    void this.checkHealth();
    void this.refreshConversations();
  }

  async checkHealth(): Promise<void> {
    this.online.set(await this.api.health());
  }

  async refreshConversations(): Promise<void> {
    try {
      const list = await this.api.conversations();
      this.conversations.set(list.conversations);
    } catch (error) {
      this.showError(error);
    }
  }

  private async open(id: string | undefined): Promise<void> {
    this.error.set('');
    if (id && id === this.justCreated) {
      this.justCreated = null;
      return;
    }
    const token = ++this.loadToken;
    if (!id) {
      this.lines.set([]);
      return;
    }
    this.loading.set(true);
    try {
      const page = await this.api.messages(id);
      if (token !== this.loadToken) return; // the user opened another conversation meanwhile
      this.lines.set(
        page.messages
          .filter((message) => message.role === 'user' || message.role === 'assistant')
          .map((message) => ({
            role: message.role as ChatLine['role'],
            content: message.content,
            meta: message.role === 'assistant' ? (message.model ?? undefined) : undefined,
          })),
      );
    } catch (error) {
      if (token === this.loadToken) this.showError(error);
    } finally {
      if (token === this.loadToken) this.loading.set(false);
    }
  }

  newChat(): void {
    void this.router.navigate(['/chat']);
  }

  openConversation(id: string): void {
    void this.router.navigate(['/chat', id]);
  }

  onKeydown(event: KeyboardEvent): void {
    // Enter sends, Shift+Enter makes a new line; ignore Enter while an IME is composing Arabic text.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void this.send();
    }
  }

  async send(): Promise<void> {
    const message = this.draft().trim();
    if (!message || this.sending()) return;
    const conversationId = this.id() ?? null;
    this.error.set('');
    this.sending.set(true);
    this.draft.set('');
    this.lines.update((lines) => [
      ...lines,
      { role: 'user', content: message },
      { role: 'assistant', content: '', pending: true },
    ]);
    try {
      const reply = await this.api.chat(message, conversationId);
      const meta = [reply.agent_name ?? reply.selected_agent_name, reply.model].filter(Boolean).join(' · ');
      this.replacePending({ role: 'assistant', content: reply.answer, meta });
      if (reply.confirmation_required) {
        this.lines.update((lines) => [
          ...lines,
          { role: 'assistant', content: 'هذا الإجراء ينتظر تأكيدك. رد بـ "نعم" للتأكيد أو "لا" للإلغاء.', meta: 'تأكيد' },
        ]);
      }
      if (!conversationId && reply.conversation_id) {
        this.justCreated = reply.conversation_id;
        void this.router.navigate(['/chat', reply.conversation_id], { replaceUrl: true });
      }
      void this.refreshConversations();
    } catch (error) {
      this.lines.update((lines) => {
        const kept = lines.filter((line) => !line.pending);
        const last = kept[kept.length - 1];
        if (last) kept[kept.length - 1] = { ...last, failed: true };
        return kept;
      });
      if (!this.draft()) this.draft.set(message); // let the user retry without retyping
      this.showError(error);
      if (error instanceof ApiError && error.status === 0) void this.checkHealth();
    } finally {
      this.sending.set(false);
    }
  }

  private replacePending(line: ChatLine): void {
    this.lines.update((lines) => lines.map((item) => (item.pending ? line : item)));
  }

  private showError(error: unknown): void {
    this.error.set(error instanceof Error ? error.message : String(error));
  }

  private scrollToEnd(): void {
    const element = this.scroller()?.nativeElement;
    if (element) element.scrollTop = element.scrollHeight;
  }
}
