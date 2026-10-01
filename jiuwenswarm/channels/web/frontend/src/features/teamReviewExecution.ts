import type { TeamMemberExecutionEvent } from '../stores/sessionStore';
import { normalizeFinalContent } from '../utils/finalContent';
import { parseTimestampToMs } from '../utils/timestamp';
import { normalizeToolCallPayload, normalizeToolResultPayload } from './tool-events/toolEventNormalizer';

function payloadOf(record: Record<string, unknown>): Record<string, unknown> {
  const nested = record.event_payload;
  return nested && typeof nested === 'object' && !Array.isArray(nested)
    ? { ...record, ...(nested as Record<string, unknown>) }
    : record;
}

function resultText(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object' && 'content' in value && Array.isArray(value.content)) {
    const text = value.content
      .filter((item) => item?.type === 'text' && typeof item.text === 'string')
      .map((item) => item.text)
      .join('\n');
    if (text) return text;
  }
  return JSON.stringify(value);
}

/** Wire roles may be projected as teammate; invocation provenance owns identity. */
export function isScheduledReviewRecord(record: Record<string, unknown>): boolean {
  return payloadOf(record).execution_kind === 'scheduled_review';
}

/** The same identity is used for live events and history, without adding a roster member. */
export function parseTeamReviewExecution(
  record: Record<string, unknown>,
  sessionId: string,
  eventType = String(record.event_type || ''),
): TeamMemberExecutionEvent | null {
  const payload = payloadOf(record);
  if (!isScheduledReviewRecord(payload)) return null;
  if (payload.session_id && payload.session_id !== sessionId) return null;
  const text = (key: string) => (typeof payload[key] === 'string' ? (payload[key] as string).trim() : '');
  const invocation = text('review_invocation_id');
  const task = text('review_task_id');
  const member = text('member_name') || text('source_member');
  const round = payload.review_round;
  if (!invocation || !task || !member || !Number.isInteger(round) || Number(round) < 1) return null;
  const kind = eventType === 'team.member_turn' ? 'final' : eventType.replace(/^chat\./, '');
  if (kind !== 'tool_call' && kind !== 'tool_result' && kind !== 'final' && kind !== 'file') return null;
  // History's top-level id is the chat record id, never the tool call identity.
  const body = { ...payload };
  delete body.id;
  const nestedTool = payload[kind];
  const tool =
    nestedTool && typeof nestedTool === 'object' && !Array.isArray(nestedTool)
      ? (nestedTool as Record<string, unknown>)
      : {};
  const toolId = [tool.tool_call_id, tool.toolCallId, tool.id, payload.tool_call_id, payload.toolCallId].find(
    (value): value is string => typeof value === 'string' && Boolean(value),
  );
  const call = kind === 'tool_call' ? normalizeToolCallPayload(body) : null;
  const result = kind === 'tool_result' ? normalizeToolResultPayload(body) : null;
  if ((kind === 'tool_call' || kind === 'tool_result') && !toolId) return null;
  const files =
    kind === 'file' && Array.isArray(payload.files)
      ? payload.files.filter(
          (file): file is { name: string } =>
            Boolean(file) && typeof file === 'object' && typeof file.name === 'string',
        )
      : undefined;
  if (kind === 'file' && !files?.length) return null;
  const timestamp = parseTimestampToMs(payload.timestamp);
  return {
    id: `team-review:${JSON.stringify([sessionId, task, round, invocation, kind, toolId || (files ? JSON.stringify(files) : '')])}`,
    member_id: member,
    kind,
    timestamp: Number.isFinite(timestamp) ? timestamp : Date.now(),
    title: '',
    content: call
      ? call.description || call.formatted_args || JSON.stringify(call.arguments)
      : result
        ? result.summary || result.result || resultText(tool.result ?? payload.result)
        : files
          ? files.map((file) => file.name).join('\n')
          : normalizeFinalContent(payload),
    tool_name: call?.name || result?.toolName,
    tool_call_id: toolId,
    ...(files ? { files } : {}),
    review: {
      task_id: task,
      round: Number(round),
      invocation_id: invocation,
      provider_id: text('provider_id'),
      member_session_id: text('member_session_id'),
      ...(text('terminal_status') ? { terminal_status: text('terminal_status') } : {}),
    },
  };
}
