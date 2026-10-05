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
  const selectedModelName = useSessionStore(
    (state) => state.runtimes[activeSessionId ?? '']?.selectedModelName ?? null,
  );
  const defaultModelName = useSessionStore((state) => state.defaultModelName);
  const setSelectedModelName = useSessionStore((state) => state.setSelectedModelName);
  // Preserve the chat.send model resolution; the shared picker does not choose defaults.
  const selected = resolveChatModelSelection(models, selectedModelName, defaultModelName, availableModels);
  const exact = isExactModelSelectionKey(selectedModelName);
  if (!selected && !exact) return null;

  return (
    <ModelPicker
      testIdPrefix="chat-panel-model-selector"
      value={exact ? selectedModelName : selected!.model_name}
      displayLabel={
        exact
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
