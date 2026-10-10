export type Status = 'todo' | 'doing' | 'done';
export type Priority = 'high' | 'normal' | 'low';
export interface Reference {
  id: string;
  available: boolean;
  title: string;
}
export interface Task {
  task_id: string;
  number: number;
  title: string;
  description: string;
  status: Status;
  priority: Priority;
  project_id: string | null;
  linked_session_id: string | null;
  result_note: string;
  version: number;
  created_at: number;
  updated_at: number;
  project: Reference | null;
  linked_session: Reference | null;
}
export interface Page {
  tasks: Task[];
  next_cursor: string | null;
}
