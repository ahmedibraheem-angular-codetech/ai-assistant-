import { Injectable, InjectionToken, inject } from '@angular/core';
import { invoke, isTauri } from '@tauri-apps/api/core';

import {
  AgentSummary,
  AnalyticsRange,
  AnalyticsSummary,
  Approval,
  ApprovalList,
  ChatReply,
  ConversationList,
  MessageList,
} from './models';

/**
 * Calls a Tauri command. Injected so tests can replace it.
 * The web view never holds the API token: Rust reads it and adds the header.
 */
export type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;

export const NOT_IN_APP = 'افتح هذه الواجهة من تطبيق الماك، فهي لا تعمل في المتصفح.';

export const TAURI_INVOKE = new InjectionToken<Invoke>('TAURI_INVOKE', {
  providedIn: 'root',
  factory: () => (command, args) =>
    isTauri() ? invoke(command, args) : Promise.reject(NOT_IN_APP),
});

interface BridgeResponse {
  status: number;
  body: unknown;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Turn a status + FastAPI body into one Arabic sentence the user can act on. */
export function describeFailure(status: number, body: unknown): string {
  const detail = detailText(body);
  switch (status) {
    case 401:
      return 'رفض الخادم الـ token. إن غيّرته (rotate)، أعد تشغيل الخادم ثم جرّب مرة أخرى.';
    case 423:
      return `المساعد متوقف مؤقتاً${detail ? `: ${detail}` : '.'}`;
    case 502:
      return `لم يرد نموذج اللغة (Ollama)${detail ? `: ${detail}` : '.'}`;
    default:
      return detail || `فشل الطلب (HTTP ${status}).`;
  }
}

function detailText(body: unknown): string {
  if (typeof body === 'string') return body;
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && 'message' in detail) return String((detail as { message: unknown }).message);
    if (Array.isArray(detail)) {
      return detail
        .map((item) => (item && typeof item === 'object' && 'msg' in item ? String(item.msg) : String(item)))
        .join('، ');
    }
  }
  return '';
}

const MESSAGE_PAGE = 200; // server maximum

@Injectable({ providedIn: 'root' })
export class AssistantApi {
  private readonly invoke = inject(TAURI_INVOKE);

  async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let response: BridgeResponse;
    try {
      response = await this.invoke<BridgeResponse>('api_request', { method, path, body: body ?? null });
    } catch (error) {
      throw new ApiError(0, typeof error === 'string' ? error : String(error));
    }
    if (response.status < 200 || response.status >= 300) {
      throw new ApiError(response.status, describeFailure(response.status, response.body));
    }
    return response.body as T;
  }

  /** True when the server answers its public health check. */
  async health(): Promise<boolean> {
    try {
      return await this.invoke<boolean>('api_health');
    } catch {
      return false;
    }
  }

  chat(message: string, conversationId?: string | null): Promise<ChatReply> {
    const body: Record<string, string> = { message };
    if (conversationId) body['conversation_id'] = conversationId;
    return this.request<ChatReply>('POST', '/api/v1/chat', body);
  }

  analytics(range: AnalyticsRange = '7d'): Promise<AnalyticsSummary> {
    return this.request<AnalyticsSummary>('GET', `/api/v1/analytics/summary?range=${range}`);
  }

  pendingApprovals(): Promise<ApprovalList> {
    return this.request<ApprovalList>('GET', '/api/v1/approvals');
  }

  approvalHistory(): Promise<ApprovalList> {
    return this.request<ApprovalList>('GET', '/api/v1/approvals/history');
  }

  /** Runs the sealed operation on the server. Only called after the user confirms. */
  approve(id: string): Promise<Approval> {
    return this.request<Approval>('POST', `/api/v1/approvals/${encodeURIComponent(id)}/approve`);
  }

  reject(id: string): Promise<Approval> {
    return this.request<Approval>('POST', `/api/v1/approvals/${encodeURIComponent(id)}/reject`);
  }

  agents(): Promise<{ agents: AgentSummary[]; count: number }> {
    return this.request('GET', '/api/v1/agents');
  }

  conversations(limit = 50): Promise<ConversationList> {
    return this.request<ConversationList>('GET', `/api/v1/conversations?limit=${limit}`);
  }

  /** The latest messages of a conversation, oldest first. */
  async messages(conversationId: string): Promise<MessageList> {
    const base = `/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`;
    let page = await this.request<MessageList>('GET', `${base}?limit=${MESSAGE_PAGE}`);
    if (page.count > page.messages.length) {
      const offset = Math.max(0, page.count - MESSAGE_PAGE);
      page = await this.request<MessageList>('GET', `${base}?limit=${MESSAGE_PAGE}&offset=${offset}`);
    }
    const messages = [...page.messages].sort((a, b) => a.sequence_number - b.sequence_number);
    return { ...page, messages };
  }
}
