"""Bounded task storage for the MOSAICO A2A server."""
import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.inmemory_task_store import resolve_user_scope
from a2a.types import Task


class MosaicoTaskStore(InMemoryTaskStore):
    """Keep a bounded, owner-scoped set of tasks and report evictions."""

    def __init__(self, max_tasks_per_owner: int, on_task_evicted: Callable[[str, str, str], Any] | None = None):
        if (isinstance(max_tasks_per_owner, bool)
                or not isinstance(max_tasks_per_owner, int) or max_tasks_per_owner < 1):
            raise ValueError("MOSAICO task store max tasks must be a positive integer")
        super().__init__()
        self.max_tasks_per_owner = max_tasks_per_owner
        self._retention_lock = asyncio.Lock()
        self._retained_tasks: dict[str, OrderedDict[str, str]] = {}
        self._on_task_evicted = on_task_evicted

    async def save(self, task: Task, context) -> None:
        owner = resolve_user_scope(context)
        async with self._retention_lock:
            await super().save(task, context)
            owner_tasks = self._retained_tasks.setdefault(owner, OrderedDict())
            owner_tasks.pop(task.id, None)
            owner_tasks[task.id] = task.context_id
            while len(owner_tasks) > self.max_tasks_per_owner:
                task_id, context_id = owner_tasks.popitem(last=False)
                await super().delete(task_id, context)
                if self._on_task_evicted is not None:
                    self._on_task_evicted(owner, task_id, context_id)

    async def delete(self, task_id: str, context) -> None:
        owner = resolve_user_scope(context)
        async with self._retention_lock:
            owner_tasks = self._retained_tasks.get(owner)
            context_id = owner_tasks.pop(task_id, None) if owner_tasks is not None else None
            if owner_tasks is not None and not owner_tasks:
                self._retained_tasks.pop(owner, None)
            await super().delete(task_id, context)
            if context_id is not None and self._on_task_evicted is not None:
                self._on_task_evicted(owner, task_id, context_id)
