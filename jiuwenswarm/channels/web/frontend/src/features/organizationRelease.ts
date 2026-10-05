import { isTeamAgentMode } from './planMode/wireMode';

/** UI release boundary only; the server remains the authorization authority. */
export function organizationRequestRestriction(
  organizationAuth: boolean,
  method: string,
  params: Record<string, unknown> = {},
  sessionMode?: string,
): 'organizationRelease.goalUnavailable' | 'organizationRelease.teamUnavailable' | null {
  if (!organizationAuth) return null;
  if ((method === 'command.goal' && (params.action ?? 'get') !== 'get') || params.attach_goal === true) {
    return 'organizationRelease.goalUnavailable';
  }
  if (['session.create', 'chat.send', 'chat.resume', 'chat.answer', 'chat.user_answer'].includes(method)
      && (isTeamAgentMode(String(params.mode ?? '')) || isTeamAgentMode(sessionMode ?? '')
        || params.is_swarm === true)) {
    return 'organizationRelease.teamUnavailable';
  }
  return null;
}
