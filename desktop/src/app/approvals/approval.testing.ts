// Test data only: an approval shaped like the server's to_public().

import { Approval } from '../core/models';

export function approval(extra: Partial<Approval> = {}): Approval {
  return {
    id: 'a1',
    agent_id: 'agent-1',
    tool_name: 'run_code',
    action: 'execute',
    resource_type: 'sandbox',
    resource_id: 'python',
    description: 'تحليل ملف المبيعات',
    change: null,
    risk: 'high',
    reason: 'Running code always needs approval.',
    mode: 'ask',
    status: 'PENDING',
    created_at: '2026-11-21T09:50:00Z',
    expires_at: '2026-11-21T10:30:00Z',
    decided_at: null,
    execution_status: null,
    result: null,
    review: {
      reviewable: true,
      verified: true,
      mcp_execution_available: false,
      arguments_text: '{"code":"print(1)"}',
      hidden: [],
      warnings: [],
      mcp: { applies: false },
    },
    ...extra,
  };
}
