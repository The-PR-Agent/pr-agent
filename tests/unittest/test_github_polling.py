from collections import deque

from pr_agent.servers import github_polling


def test_start_queued_processes_respects_parallel_limit(monkeypatch):
    created_processes = []

    class FakeProcess:
        def __init__(self, target, args):
            self.target = target
            self.args = args
            self.started = False
            created_processes.append(self)

        def start(self):
            self.started = True

    monkeypatch.setattr(github_polling.multiprocessing, "Process", FakeProcess)

    task_queue = deque((lambda: None, ()) for _ in range(12))
    processes = github_polling._start_queued_processes(task_queue, 10)

    assert len(created_processes) == 10
    assert len(processes) == 10
    assert all(process.started for process in processes)
    assert not task_queue
