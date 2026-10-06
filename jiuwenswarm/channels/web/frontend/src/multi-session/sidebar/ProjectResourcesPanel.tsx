import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input } from '../../components/ui';
import { SettingRow } from '../../features/settings/components/SettingRow';
import { SettingsSection } from '../../features/settings/components/SettingsSection';
import { projectRegistryClient } from '../../features/workspace/projectRegistryClient';
import {
  ResourceMutationCommittedError,
  type ProjectResources,
  type ResourceMutationInput,
} from '../../features/workspace/projectResourceClient';
import { onOrganizationCredentialChange } from '../../services/organizationCredentialEvents';
import { webClient } from '../../services/webClient';

export function ProjectResourcesPanel({ projectId }: { projectId: string }) {
  const { t } = useTranslation();
  const [data, setData] = useState<ProjectResources | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [resourceId, setResourceId] = useState('');
  const [target, setTarget] = useState('');
  const [selectedActions, setSelectedActions] = useState<string[]>([]);
  const [expires, setExpires] = useState('');
  const generation = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const key = (name: string) => `multiSession.project.resources.${name}`;
  const selected = data?.resources.find((item) => item.resource_id === resourceId && item.can_grant);

  function clearForm() {
    setResourceId('');
    setTarget('');
    setSelectedActions([]);
    setExpires('');
  }
  function invalidate() {
    generation.current += 1;
    pending.current?.abort();
    inFlight.current = false;
    setData(null);
    clearForm();
    setBusy(false);
    setNotice('invalidated');
  }
  async function refresh(feedback: string | null = null) {
    const current = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    inFlight.current = true;
    setBusy(true);
    setData(null);
    clearForm();
    setNotice(feedback);
    try {
      const result = await projectRegistryClient.listResources(projectId, controller.signal);
      if (current !== generation.current || controller.signal.aborted) return;
      setData(result);
    } catch {
      if (current === generation.current && !controller.signal.aborted)
        setNotice(feedback === 'exitUnconfirmed' ? 'exitUnconfirmedRefreshFailed' : 'loadFailed');
    } finally {
      if (current === generation.current) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  }
  useEffect(() => {
    void refresh();
    const auth = onOrganizationCredentialChange(invalidate);
    const connection = webClient.onStateChange((state) => {
      if (state !== 'ready') invalidate();
    });
    return () => {
      auth();
      connection();
      generation.current += 1;
      pending.current?.abort();
    };
    // A project owns this panel's request lifetime, never a form field.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function mutate(operation: 'grant' | 'revoke', id: string, actor: string) {
    if (!data || inFlight.current) return;
    const resource = data.resources.find((item) => item.resource_id === id);
    if (
      !resource ||
      (operation === 'grant'
        ? !resource.can_grant
        : !resource.grants.some((grant) => grant.target_actor === actor && grant.can_revoke))
    )
      return;
    const expiresAt = expires ? new Date(expires).getTime() / 1000 : null;
    if (
      operation === 'grant' &&
      (!actor.trim() ||
        selectedActions.length === 0 ||
        (expiresAt !== null &&
          (!Number.isFinite(expiresAt) ||
            expiresAt <= Date.now() / 1000 ||
            (resource.expires_at !== null && expiresAt > resource.expires_at))))
    ) {
      setNotice('invalidInput');
      return;
    }
    const input: ResourceMutationInput = {
      project_id: projectId,
      resource_id: id,
      target_actor: actor.trim(),
      expected_acl_revision: data.acl_revision,
      expected_resource_revision: data.resource_revision,
    };
    const current = ++generation.current;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    inFlight.current = true;
    setBusy(true);
    setNotice(null);
    try {
      if (operation === 'grant')
        await projectRegistryClient.grantResource(
          { ...input, actions: [...selectedActions], expires_at: expiresAt },
          controller.signal,
        );
      else await projectRegistryClient.revokeResource(input, controller.signal);
      if (current === generation.current && !controller.signal.aborted) await refresh('saved');
    } catch (failure) {
      if (current !== generation.current || controller.signal.aborted) return;
      if (failure instanceof ResourceMutationCommittedError) await refresh('exitUnconfirmed');
      else if ((failure as { code?: string })?.code === 'CONFLICT') await refresh('conflict');
      else {
        // Unknown outcomes are never automatically retried. Reload authority before another mutation.
        setData(null);
        clearForm();
        setNotice('mutationFailed');
      }
    } finally {
      if (current === generation.current) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  }

  const formatExpiry = (value: number | null) =>
    value === null ? t(key('noExpiry')) : new Date(value * 1000).toLocaleString();
  return (
    <section
      className="project-content-dialog__resources"
      data-testid="multi-session-project-resources"
      data-variant={projectId}
    >
      <p data-testid="multi-session-project-resources-description">{t(key('description'))}</p>
      {notice && (
        <p role="status" data-testid="multi-session-project-resources-notice" data-variant={notice}>
          {t(key(notice))}
        </p>
      )}
      {busy && (
        <p role="status" data-testid="multi-session-project-resources-loading">
          {t('common.loading')}
        </p>
      )}
      <Button disabled={busy} onClick={() => void refresh()} data-testid="multi-session-project-resources-refresh">
        {t('multiSession.project.content.reload')}
      </Button>
      {data && data.resources.length === 0 && (
        <p data-testid="multi-session-project-resources-empty">{t(key('empty'))}</p>
      )}
      {data && data.resources.length > 0 && (
        <>
          <SettingsSection separatedRows>
            {data.resources.map((resource) => (
              <div
                key={resource.resource_id}
                data-testid="multi-session-project-resources-row"
                data-variant={resource.resource_id}
              >
                <SettingRow
                  title={resource.resource_id}
                  description={t(key(`kinds.${resource.kind}`))}
                  subSettings={
                    <div className="project-content-dialog__resource-details">
                      <p data-testid="multi-session-project-resources-authority">
                        {t(key('authority'), {
                          actions:
                            resource.actions.map((action) => t(key(`actions.${action}`))).join(', ') || t(key('none')),
                          expires: formatExpiry(resource.expires_at),
                        })}
                      </p>
                      {resource.grants.map((grant) => (
                        <div
                          className="project-content-dialog__grant"
                          key={grant.target_actor}
                          data-testid="multi-session-project-resources-grant"
                          data-variant={grant.target_actor}
                        >
                          <span>
                            {grant.target_actor} ·{' '}
                            {grant.actions.map((action) => t(key(`actions.${action}`))).join(', ')} ·{' '}
                            {formatExpiry(grant.expires_at)} · {t(key(grant.state))}
                          </span>
                          {grant.can_revoke && (
                            <Button
                              disabled={busy}
                              onClick={() => void mutate('revoke', resource.resource_id, grant.target_actor)}
                              data-testid="multi-session-project-resources-revoke"
                            >
                              {t(key('revoke'))}
                            </Button>
                          )}
                        </div>
                      ))}
                    </div>
                  }
                />
              </div>
            ))}
          </SettingsSection>
          {data.resources.some((item) => item.can_grant) && (
            <fieldset disabled={busy} data-testid="multi-session-project-resources-form">
              <legend>{t(key('grant'))}</legend>
              <label className="project-content-dialog__field">
                {t(key('resource'))}
                <select
                  value={resourceId}
                  onChange={(event) => {
                    setResourceId(event.target.value);
                    setSelectedActions([]);
                    setExpires('');
                  }}
                  data-testid="multi-session-project-resources-select"
                >
                  <option value="">{t(key('choose'))}</option>
                  {data.resources
                    .filter((item) => item.can_grant)
                    .map((item) => (
                      <option key={item.resource_id} value={item.resource_id}>
                        {item.resource_id}
                      </option>
                    ))}
                </select>
              </label>
              {selected && (
                <>
                  <label className="project-content-dialog__field">
                    {t(key('target'))}
                    <Input value={target} onChange={setTarget} data-testid="multi-session-project-resources-target" />
                  </label>
                  <div
                    className="project-content-dialog__actions"
                    data-testid="multi-session-project-resources-action-options"
                  >
                    {selected.actions.map((action) => (
                      <label key={action}>
                        <input
                          type="checkbox"
                          checked={selectedActions.includes(action)}
                          onChange={(event) =>
                            setSelectedActions((items) =>
                              event.target.checked ? [...items, action] : items.filter((item) => item !== action),
                            )
                          }
                          data-testid="multi-session-project-resources-action"
                          data-variant={action}
                        />
                        {t(key(`actions.${action}`))}
                      </label>
                    ))}
                  </div>
                  <label className="project-content-dialog__field">
                    {t(key('expiry'))}
                    <Input
                      type="datetime-local"
                      value={expires}
                      onChange={setExpires}
                      data-testid="multi-session-project-resources-expiry"
                    />
                  </label>
                  <p data-testid="multi-session-project-resources-inherited">{t(key('inherited'))}</p>
                  <Button
                    variant="primary"
                    disabled={busy || !target.trim() || selectedActions.length === 0}
                    onClick={() => void mutate('grant', selected.resource_id, target)}
                    data-testid="multi-session-project-resources-submit"
                  >
                    {t(key('grant'))}
                  </Button>
                </>
              )}
            </fieldset>
          )}
        </>
      )}
    </section>
  );
}
