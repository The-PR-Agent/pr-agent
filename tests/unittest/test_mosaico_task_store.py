"""Tests for bounded MOSAICO task retention."""
from collections import deque
from types import SimpleNamespace

import pytest
from a2a.types import Task, TaskState, TaskStatus

from pr_agent.mosaico.task_store import MosaicoTaskStore


def _context(owner: str):
    return SimpleNamespace(user=SimpleNamespace(user_name=owner))


def _task(task_id: str, context_id: str):
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
    )


@pytest.mark.asyncio
async def test_store_eviction_removes_old_tasks_and_reports_index_data():
    evicted = []
    store = MosaicoTaskStore(max_tasks_per_owner=2, on_task_evicted=lambda *item: evicted.append(item))
    context = _context("alice")

    await store.save(_task("task-1", "context-1"), context)
    await store.save(_task("task-2", "context-2"), context)
    await store.save(_task("task-3", "context-3"), context)

    assert await store.get("task-1", context) is None
    assert await store.get("task-2", context) is not None
    assert await store.get("task-3", context) is not None
    assert evicted == [("alice", "task-1", "context-1")]


@pytest.mark.asyncio
async def test_store_eviction_prunes_the_executor_follow_up_index():
    from pr_agent.mosaico.executor import PRAgentExecutor

    executor = PRAgentExecutor()
    store = MosaicoTaskStore(max_tasks_per_owner=1, on_task_evicted=executor.forget_task)
    executor._recent_task_ids[("alice", "context-1")] = deque(["task-1"])
    context = _context("alice")

    await store.save(_task("task-1", "context-1"), context)
    await store.save(_task("task-2", "context-2"), context)

    assert ("alice", "context-1") not in executor._recent_task_ids


@pytest.mark.asyncio
async def test_store_eviction_is_scoped_per_owner():
    store = MosaicoTaskStore(max_tasks_per_owner=1)
    alice = _context("alice")
    bob = _context("bob")

    await store.save(_task("alice-1", "alice-context"), alice)
    await store.save(_task("bob-1", "bob-context"), bob)
    await store.save(_task("alice-2", "alice-context-2"), alice)

    assert await store.get("alice-1", alice) is None
    assert await store.get("alice-2", alice) is not None
    assert await store.get("bob-1", bob) is not None


@pytest.mark.asyncio
async def test_explicit_delete_reports_removed_task():
    evicted = []
    store = MosaicoTaskStore(max_tasks_per_owner=2, on_task_evicted=lambda *item: evicted.append(item))
    context = _context("alice")
    await store.save(_task("task-1", "context-1"), context)

    await store.delete("task-1", context)

    assert evicted == [("alice", "task-1", "context-1")]
