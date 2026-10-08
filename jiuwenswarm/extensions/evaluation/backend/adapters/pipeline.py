"""Reuse the public core task/result values without serializing runtime credentials."""

from openjiuwen.agent_evolving.evaluator.evaluator_pipeline import EvalResult, Task

from ..models import TaskDraft


def core_task(task: TaskDraft) -> Task:
    return Task(
        task_id=task.task_id,
        instruction=task.instruction,
        environment_spec={
            "type": task.environment,
            "test_timeout": task.acceptance.timeout_seconds,
        },
        metadata={"acceptance_policy": "shared-environment-v1"},
    )


def core_result(*, returncode: int, stdout: str, stderr: str) -> EvalResult:
    return EvalResult(
        passed=returncode == 0,
        pass_rate=1.0 if returncode == 0 else 0.0,
        test_output=stdout + stderr,
        returncode=returncode,
        test_details={"policy": "shared-environment-v1"},
    )
