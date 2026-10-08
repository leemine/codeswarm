import { useTranslation } from 'react-i18next';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { useChatStore } from '../../stores/chatStore';
import { isExactModelSelectionKey, resolveChatModelSelection, useSessionStore } from '../../stores/sessionStore';
import ModelPicker from '../ModelPicker';

function openModelSettings(): void {
  requestSettingsModule('models');
}

export default function ChatModelSelector({ disabled = false }: { disabled?: boolean }): JSX.Element | null {
  const { t } = useTranslation();
  const availableModels = useSessionStore((state) => state.availableModels);
  const models = useSessionStore((state) => state.chatAvailableModels);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const choice = useSessionStore((state) => state.runtimes[activeSessionId ?? '']?.executionChoice);
  const allowed = activeSessionId === 'new' ? choice?.model_selection_keys : undefined;
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
  );
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const setSelectedModelName = useSessionStore((state) => state.setSelectedModelName);
  // Preserve the chat.send model resolution; the shared picker does not choose defaults.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName, availableModels);
  const exact = isExactModelSelectionKey(selectedModelName);
  if (!selected && !exact && !allowed) return null;
  const incompatible = allowed && !allowed.includes(selected?.selection_key || selectedModelName || '');

  return (
    <ModelPicker
      testIdPrefix="chat-panel-model-selector"
      value={incompatible ? null : exact ? selectedModelName : (selected?.model_name ?? null)}
      allowedSelectionKeys={allowed}
      displayLabel={
        incompatible
          ? t('executionPicker.chooseModel')
          : exact
            ? selected
              ? selected.alias || selected.model_name
              : t('chat.modelSelector.unavailable', { model: selectedModelName })
            : undefined
      }
      onChange={(modelName) => {
        if (activeSessionId) setSelectedModelName(activeSessionId, modelName);
      }}
      disabled={disabled}
      onAddModel={openModelSettings}
    />
  );
}
