import { approvable, changeTitle, hiddenCount, outputText, risk, status, timeLeft } from './approvals-view';
import { approval } from './approval.testing';

const NOW = new Date('2026-11-21T10:00:00Z');

describe('approvals rules', () => {
  it('offers approve only for a verified, reviewable, pending request', () => {
    expect(approvable(approval())).toEqual({ ok: true, why: '' });
    expect(approvable(approval({ status: 'COMPLETED' })).ok).toBe(false);
    const tampered = approval();
    tampered.review = { ...tampered.review, verified: false };
    expect(approvable(tampered)).toEqual({ ok: false, why: 'العملية المخزنة لا تطابق بصمتها. ارفضها.' });
    const unsafe = approval();
    unsafe.review = { ...unsafe.review, reviewable: false };
    expect(approvable(unsafe).why).toContain('لا يمكن مراجعة');
  });

  it('never offers to run MCP operations from here', () => {
    const mcp = approval({ tool_name: 'mcp_call' });
    mcp.review = { ...mcp.review, mcp: { applies: true }, mcp_execution_available: false };
    expect(approvable(mcp).ok).toBe(false);
    expect(approvable(mcp).why).toContain('MCP');
  });

  it('labels risk, status and time left in Arabic', () => {
    expect(risk('high')).toEqual({ label: 'خطورة عالية', tone: 'danger' });
    expect(risk('low').tone).toBe('ok');
    expect(status(approval({ status: 'REJECTED' })).label).toBe('رفضتها');
    expect(status(approval({ status: 'FAILED', outcome: 'unknown' })).label).toBe('النتيجة غير معروفة');
    expect(timeLeft('2026-11-21T10:30:00Z', NOW)).toBe('تنتهي خلال 30 د');
    expect(timeLeft('2026-11-21T12:30:00Z', NOW)).toBe('تنتهي خلال 2 س');
    expect(timeLeft('2026-11-21T09:00:00Z', NOW)).toBe('انتهت المهلة');
    expect(timeLeft(null, NOW)).toBe('');
  });

  it('describes file changes and hidden values', () => {
    const change = approval({ change: { operation: 'delete', relative_path: 'notes/a.md', size_bytes: 12, diff: '' } });
    expect(changeTitle(change)).toBe('نقل ملف إلى الاسترجاع: notes/a.md');
    expect(changeTitle(approval())).toBeNull();
    const secret = approval();
    secret.review = { ...secret.review, hidden: ['token', 'password'] };
    expect(hiddenCount(secret)).toBe(2);
  });

  it('shortens long tool output', () => {
    expect(outputText({ ok: 1 })).toBe('{"ok":1}');
    expect(outputText('x'.repeat(700))).toHaveLength(601);
    expect(outputText(null)).toBe('');
  });
});
