import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useTranslation,
} from '../../../channels/web/frontend/src/applicationPlugins/ui';
import { Button, Input, Select } from '../../../channels/web/frontend/src/components/ui';
import { evaluationRequest as request } from './client';
import type { Experiment, Options, TaskRef } from './types';

export default function Experiments({
  selected,
  onSelect,
}: {
  selected: TaskRef[];
  onSelect: (tasks: TaskRef[]) => void;
}) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<Options>();
  const [model, setModel] = useState('');
  const [profile, setProfile] = useState('');
  const [name, setName] = useState('');
  const [timeout, setTimeout] = useState('300');
  const [repeats, setRepeats] = useState('1');
  const [acknowledged, setAcknowledged] = useState(false);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [detail, setDetail] = useState<Experiment>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const key = useRef(crypto.randomUUID());
  const mounted = useRef(true);
  const refresh = useCallback(async () => {
    const { experiments: values } = await request<{ experiments: Experiment[] }>('experiment.list');
    if (!mounted.current) return;
    setExperiments(values);
    setDetail((current) => (current ? values.find((item) => item.id === current.id) : values[0]));
  }, []);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await operation();
    } catch (err) {
      setError((err as { code?: string }).code || (err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    void request<Options>('options')
      .then((value) => {
        if (!mounted.current) return;
        setOptions(value);
        setModel(value.models[0]?.selection_key || '');
        setProfile(value.profiles[0]?.id || '');
      })
      .catch((err) => {
        if (mounted.current) setError(err.code || err.message);
      });
    let timer: ReturnType<typeof globalThis.setTimeout>;
    const poll = async () => {
      try {
        await refresh();
      } catch (err) {
        if (mounted.current) setError((err as Error).message);
      }
      if (mounted.current) timer = globalThis.setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => {
      mounted.current = false;
      globalThis.clearTimeout(timer);
    };
  }, [refresh]);
  const create = async () => {
    const result = await request<Experiment>('experiment.create', {
      idempotency_key: key.current,
      experiment: {
        name,
        tasks: selected,
        model,
        execution_profile_id: profile,
        repeats: Number(repeats),
        timeout_seconds: Number(timeout),
        shared_environment_acknowledged: acknowledged,
      },
    });
    key.current = crypto.randomUUID();
    await refresh();
    setDetail(result);
  };
  const exportEvidence = async (experiment: Experiment) => {
    // Fetch again through the authenticated host channel; cached UI is not authority.
    const fresh = await request<Experiment>('evidence', {
      experiment_id: experiment.id,
    });
    const url = URL.createObjectURL(new Blob([JSON.stringify(fresh, null, 2)], { type: 'application/json' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `evaluation-${experiment.id}.json`;
    anchor.click();
    globalThis.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const status = (value: string) => t(`evaluation.status.${value}`, { defaultValue: value });
  return (
    <section className="evaluation-panel evaluation-experiments" data-testid="evaluation-experiments">
      <h2 data-testid="evaluation-config-title">{t('evaluation.configure')}</h2>
      <p data-testid="evaluation-selected-tasks">
        {t('evaluation.selected', { count: selected.length })}:{' '}
        {selected.map((item) => `${item.task_id} v${item.revision}`).join(', ')}
      </p>
      {error && (
        <p role="alert" className="evaluation-error" data-testid="evaluation-experiment-error">
          {error}
        </p>
      )}
      <fieldset disabled={busy} className="evaluation-config" data-testid="evaluation-config-fields">
        <label>
          {t('evaluation.name')}
          <Input data-testid="evaluation-experiment-name" value={name} onChange={setName} />
        </label>
        <label>
          {t('evaluation.model')}
          <Select
            data-testid="evaluation-model"
            value={model}
            options={
              options?.models.map((item) => ({
                value: item.selection_key,
                label: item.display_name,
              })) || []
            }
            onChange={setModel}
          />
        </label>
        <label>
          {t('evaluation.profile')}
          <Select
            data-testid="evaluation-profile"
            value={profile}
            options={
              options?.profiles.map((item) => ({
                value: item.id,
                label: `${item.id} · ${item.revision}`,
              })) || []
            }
            onChange={setProfile}
          />
        </label>
        <label>
          {t('evaluation.repeats')}
          <Input type="number" min={1} max={5} data-testid="evaluation-repeats" value={repeats} onChange={setRepeats} />
        </label>
        <label>
          {t('evaluation.timeout')}
          <Input
            type="number"
            min={10}
            max={1800}
            data-testid="evaluation-timeout"
            value={timeout}
            onChange={setTimeout}
          />
        </label>
      </fieldset>
      <label className="evaluation-ack">
        <input
          type="checkbox"
          data-testid="evaluation-acknowledge"
          checked={acknowledged}
          onChange={(event) => setAcknowledged(event.target.checked)}
        />
        <span>{t('evaluation.acknowledge')}</span>
      </label>
      <p data-testid="evaluation-cost-hint">{t('evaluation.costHint')}</p>
      {options && !options.execution_available && (
        <p data-testid="evaluation-execution-unavailable">{t('evaluation.localOnly')}</p>
      )}
      <Button
        variant="primary"
        data-testid="evaluation-create"
        disabled={
          busy ||
          !selected.length ||
          !name.trim() ||
          !model ||
          !profile ||
          !acknowledged ||
          !options?.execution_available
        }
        onClick={() => void act(create)}
      >
        {t('evaluation.create')}
      </Button>
      <h2 data-testid="evaluation-runs-title">{t('evaluation.runs')}</h2>
      <div className="evaluation-run-layout">
        <ul className="evaluation-list" data-testid="evaluation-run-list">
          {experiments.map((experiment) => (
            <li key={experiment.id} data-testid="evaluation-run" data-variant={experiment.id}>
              <Button data-testid="evaluation-run-open" onClick={() => setDetail(experiment)}>
                {experiment.definition.name}
              </Button>
              <span>
                {experiment.statistics?.passed || 0}/{experiment.statistics?.planned_trials || experiment.trials.length}
              </span>
            </li>
          ))}
        </ul>
        {detail && (
          <article data-testid="evaluation-run-detail">
            <h3 data-testid="evaluation-run-name">{detail.definition.name}</h3>
            <p data-testid="evaluation-frozen-hint">
              {t('evaluation.frozen')} · {detail.definition.model} · {detail.definition.acceptance_policy}
            </p>
            <div className="evaluation-actions">
              <Button
                variant="primary"
                data-testid="evaluation-start"
                disabled={
                  busy ||
                  detail.active ||
                  !detail.trials.some((trial) => trial.attempts.some((attempt) => attempt.phase === 'pending'))
                }
                onClick={() =>
                  void act(async () => {
                    setDetail(
                      await request('experiment.start', {
                        experiment_id: detail.id,
                      }),
                    );
                    await refresh();
                  })
                }
              >
                {t('evaluation.start')}
              </Button>
              <Button
                data-testid="evaluation-cancel"
                disabled={
                  busy || detail.trials.every((trial) => trial.attempts.every((attempt) => attempt.phase === 'settled'))
                }
                onClick={() =>
                  void act(async () => {
                    setDetail(
                      await request('experiment.cancel', {
                        experiment_id: detail.id,
                      }),
                    );
                    await refresh();
                  })
                }
              >
                {t('evaluation.cancel')}
              </Button>
              <Button
                data-testid="evaluation-export"
                disabled={busy}
                onClick={() => void act(() => exportEvidence(detail))}
              >
                {t('evaluation.export')}
              </Button>
              <Button
                data-testid="evaluation-copy"
                disabled={busy}
                onClick={() => {
                  onSelect(detail.definition.tasks);
                  setName(`${detail.definition.name} — ${t('evaluation.copy')}`);
                  setModel(detail.definition.model);
                  setProfile(detail.definition.execution_profile_id);
                  setTimeout(String(detail.definition.timeout_seconds));
                  setRepeats(String(detail.definition.repeats));
                  key.current = crypto.randomUUID();
                }}
              >
                {t('evaluation.copy')}
              </Button>
            </div>
            <p data-testid="evaluation-summary">
              {t(detail.statistics?.all_settled ? 'evaluation.finalRatio' : 'evaluation.partialRatio', {
                passed: detail.statistics?.passed || 0,
                total: detail.trials.length,
              })}
            </p>
            <p data-testid="evaluation-usage">{t('evaluation.unknownUsage')}</p>
            <ul className="evaluation-trials" data-testid="evaluation-trials">
              {detail.trials.map((trial) => (
                <li key={trial.id} data-testid="evaluation-trial" data-variant={trial.id}>
                  <h3>
                    {trial.task_id} ·{' '}
                    {t('evaluation.repeatIndex', {
                      index: trial.repeat_index + 1,
                    })}
                  </h3>
                  {trial.attempts.map((attempt) => (
                    <div key={attempt.id} data-testid="evaluation-attempt" data-variant={attempt.id}>
                      <p data-testid="evaluation-attempt-status">
                        {status(
                          attempt.body.status === 'stopping'
                            ? 'stopping'
                            : attempt.body.status === 'recovery_required'
                              ? 'recovery_required'
                              : attempt.body.runtime?.state || attempt.body.status || attempt.phase,
                        )}{' '}
                        · {status(attempt.body.outcome || 'not_evaluated')}
                      </p>
                      {attempt.body.session_id && (
                        <a
                          data-testid="evaluation-session-link"
                          href={`/chat/${encodeURIComponent(attempt.body.session_id)}`}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {t('evaluation.session')}
                        </a>
                      )}
                      {attempt.body.error_code && (
                        <p data-testid="evaluation-attempt-error">{attempt.body.error_code}</p>
                      )}
                      <details data-testid="evaluation-evidence">
                        <summary data-testid="evaluation-evidence-toggle">{t('evaluation.evidence')}</summary>
                        <p data-testid="evaluation-exit">
                          {t('evaluation.exitConfirmed')}:{' '}
                          {t(attempt.body.exit_confirmed ? 'evaluation.yes' : 'evaluation.no')}
                        </p>
                        <p data-testid="evaluation-workspace">{attempt.body.workspace}</p>
                        {attempt.body.test_output !== undefined && (
                          <pre data-testid="evaluation-test-output">
                            {attempt.body.test_output || t('evaluation.emptyOutput')}
                          </pre>
                        )}
                        {attempt.body.files?.map((file) => (
                          <div key={file.path} data-testid="evaluation-delivery" data-variant={file.path}>
                            <p>
                              {file.path} · {file.status} · {file.sha256}
                            </p>
                            <pre data-testid="evaluation-diff">{file.diff}</pre>
                          </div>
                        ))}
                        <pre data-testid="evaluation-attempt-json">{JSON.stringify(attempt.body, null, 2)}</pre>
                      </details>
                    </div>
                  ))}
                </li>
              ))}
            </ul>
            <details data-testid="evaluation-snapshot">
              <summary data-testid="evaluation-snapshot-toggle">{t('evaluation.snapshot')}</summary>
              <pre data-testid="evaluation-snapshot-json">
                {JSON.stringify({ definition: detail.definition, versions: detail.versions }, null, 2)}
              </pre>
            </details>
          </article>
        )}
      </div>
    </section>
  );
}
