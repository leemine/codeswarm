import { TeamMembersPanel } from '../src/components/teamArea/TeamMembersPanel';
// Opt-in browser fixture: existing authorization components and transports.
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { useWebSocket } from '../src/hooks/useWebSocket';
import { AuthorizationPrompt } from '../src/components/InteractionSlot/AuthorizationPrompt';
import { useChatStore, useSessionStore, useWorkspaceStore } from '../src/stores';
import { webClient } from '../src/services/webClient';
import { parseTeamHistoryPanelRecords } from '../src/features/teamHistoryPanelRestore';
import i18n from '../src/i18n';

const sid = 'team-local';
useChatStore.getState().ensureRuntime(sid);
useChatStore.getState().setActiveSessionId(sid);
useSessionStore.getState().ensureRuntime(sid);
useSessionStore.getState().setMode(sid, 'team');
useWorkspaceStore.setState({ workMode: 'code' });
const report = { answers: [] as boolean[], errors: [] as string[], history: null as unknown, live: null as unknown };
void i18n.changeLanguage('zh');
function Host() {
  const api = useWebSocket({ activeSessionId: sid, onError: (error) => report.errors.push(error) });
  const pending = useChatStore((state) => state.runtimes[sid]?.pendingQuestions[0]);
  const runtime = useSessionStore((state) => state.runtimes[sid]);
  return (
    <>
      <TeamMembersPanel
        variant="expanded"
        members={runtime.teamMembers}
        tasks={runtime.teamTasks}
        selectedMemberId="worker"
      />
      {pending ? (
        <AuthorizationPrompt
          pending={pending}
          onSubmit={async (...args) => {
            const result = await api.sendUserAnswer(sid, ...args);
            report.answers.push(result);
            return result;
          }}
        />
      ) : null}
    </>
  );
}
createRoot(document.getElementById('root')!).render(
  <I18nextProvider i18n={i18n}>
    <Host />
  </I18nextProvider>,
);
async function restore() {
  report.live = JSON.parse(JSON.stringify(useSessionStore.getState().getRuntime(sid)));
  try {
    const raw = await webClient.request<{ records: Record<string, unknown>[] }>('team.history.get', {
      session_id: sid,
      limit: 500,
    });
    const restored = parseTeamHistoryPanelRecords(raw.records, sid);
    report.history = { raw, restored };
    const store = useSessionStore.getState();
    store.setTeamMembers(sid, restored.members);
    store.setTeamTasks(sid, restored.tasks);
    store.setTeamTaskEvents(sid, restored.taskEvents);
    store.setTeamMemberExecutionEvents(sid, restored.executionEvents);
  } catch (error) {
    report.errors.push(String(error));
  }
  const output = document.createElement('pre');
  output.dataset.testid = 'fixture-report';
  output.textContent = JSON.stringify(report);
  document.body.append(output);
}
webClient.on('test.done', restore);
if (new URL(location.href).searchParams.has('restore')) void restore();
else void webClient.request('team.history.get', { session_id: sid, mode: 'team.code.normal', limit: 1 });
