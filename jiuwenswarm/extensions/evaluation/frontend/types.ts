export interface Task {
  schema_version?: 1;
  task_id: string;
  name: string;
  instruction: string;
  files?: { path: string; content: string; executable?: boolean }[];
  deliverables?: string[];
  acceptance?: {
    kind: 'manual' | 'python';
    script?: string;
    timeout_seconds?: number;
    dependency_lock?: string | null;
  };
  environment?: 'shared-host-v1';
}
export interface TaskVersion {
  id: string;
  revision: number;
  digest: string;
  value: Task;
}
export interface TaskRef {
  task_id: string;
  revision: number;
}
export interface Catalog {
  schema_version: 1;
  drafts: { draft_revision: number; task: Task }[];
  tasks: TaskVersion[];
  datasets: {
    id: string;
    revision: number;
    value: { name: string; tasks: TaskRef[] };
  }[];
}
export interface ImportPreview {
  can_import: boolean;
  rows: {
    line: number;
    task?: Task;
    errors: { code: string; field: string }[];
  }[];
}
export interface Options {
  models: { selection_key: string; display_name: string }[];
  profiles: { id: string; revision: string }[];
  execution_available: boolean;
  acceptance_policies?: string[];
}
export interface Attempt {
  id: string;
  phase: string;
  body: {
    session_id?: string;
    outcome?: string;
    status?: string;
    exit_confirmed?: boolean;
    error_code?: string;
    workspace?: string;
    acceptance_policy?: string;
    authority_sha256?: string;
    verifier_removed?: boolean;
    authoritative_assertions?: number;
    verification_environment?: { image_id: string; network: string };
    test_output?: string;
    runtime?: { state: string };
    files?: { path: string; status: string; sha256?: string; diff?: string }[];
  };
}
export interface Experiment {
  id: string;
  active?: boolean;
  definition: {
    name: string;
    model: string;
    execution_profile_id: string;
    timeout_seconds: number;
    repeats: number;
    acceptance_policy: string;
    tasks: TaskRef[];
  };
  versions?: unknown;
  statistics?: { passed: number; planned_trials: number; all_settled: boolean };
  trials: {
    id: string;
    task_id: string;
    repeat_index: number;
    attempts: Attempt[];
  }[];
}
