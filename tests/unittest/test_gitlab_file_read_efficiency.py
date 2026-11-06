import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread, current_thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from requests.exceptions import ConnectionError

from pr_agent.git_providers.gitlab_provider import GitLabProvider


def test_get_pr_file_content_uses_lazy_project_handle():
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/repo"

    file_obj = MagicMock()
    file_obj.decode.return_value = b"hello\n"
    project = MagicMock()
    project.files.get.return_value = file_obj
    projects = MagicMock()
    projects.get.return_value = project
    provider.gl = SimpleNamespace(projects=projects)

    assert provider.get_pr_file_content("src/app.py", "deadbeef") == "hello\n"

    projects.get.assert_called_once_with("group/repo", lazy=True)
    project.files.get.assert_called_once_with("src/app.py", "deadbeef")


def _diff_provider(files, *, mixed=False, prefetch_failure=None, invalid_blob=None, missing_patch=False,
                   blob_mode="100644"):
    """Exercise the complete GitLab MR diff path with only the HTTP transport replaced."""
    changes = []
    for index in range(files):
        kind = ("modified", "modified", "added", "deleted", "renamed")[index % 5] if mixed else "modified"
        changes.append({
            "old_path": f"src/old{index}.py" if kind == "renamed" else f"src/file{index}.py",
            "new_path": f"src/file{index}.py",
            "diff": "" if missing_patch and index == 0 else "@@ -1 +1 @@\n-before\n+after\n",
            "new_file": kind == "added",
            "deleted_file": kind == "deleted",
            "renamed_file": kind == "renamed",
        })

    metadata = {
        "sha": "head-sha",
        "diff_refs": {"base_sha": "base-sha", "head_sha": "head-sha"},
        "changes_count": str(files),
    }

    def contents(path, ref):
        return f"{ref}: {path}\n"

    def get_file(path, ref):
        return SimpleNamespace(decode=lambda: contents(path, ref).encode())

    def response(data):
        payload = json.dumps(data).encode()
        return SimpleNamespace(
            raw=SimpleNamespace(connection=SimpleNamespace(sock=MagicMock()),
                                read1=MagicMock(side_effect=[payload, b""]), release_conn=MagicMock()),
            close=MagicMock(),
        )

    def graphql(*_args, **kwargs):
        if prefetch_failure == "transport":
            raise ConnectionError("GitLab GraphQL endpoint unavailable")
        if prefetch_failure == "graphql":
            return response({"errors": [{"message": "unsupported"}]})
        if prefetch_failure == "malformed":
            return response({"data": {"project": {"repository": {}}}})

        variables = kwargs["post_data"]["variables"]
        nodes = []
        for path in variables["paths"]:
            content = contents(path, variables["ref"])
            digest = hashlib.sha1(b"blob %d\0" % len(content.encode()) + content.encode()).hexdigest()
            nodes.append({
                "path": path,
                "oid": "bad-oid" if path == invalid_blob else digest,
                "mode": blob_mode,
                "rawTextBlob": content,
            })
        return response({"data": {"project": {"repository": {"blobs": {
            "nodes": nodes,
        }}}}})

    project = SimpleNamespace(files=SimpleNamespace(get=MagicMock(side_effect=get_file)))
    gl = SimpleNamespace(
        url="https://gitlab.example.com",
        projects=SimpleNamespace(get=lambda *a, **kw: project),
        http_get=MagicMock(return_value=metadata),
        http_list=MagicMock(return_value=changes),
        http_request=MagicMock(side_effect=graphql),
    )
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_project = "group/repo"
    provider.id_mr = 42
    provider.gl = gl
    provider.diff_files = None
    provider._merge_request_changes_cache = None
    return provider, gl, project


@pytest.mark.parametrize(("count", "batched_requests", "individual_reads"), [
    (1, 0, 2),
    (3, 0, 6),
    (5, 2, 0),
    (10, 2, 0),
    (49, 10, 0),
])
def test_gitlab_diff_content_batching_preserves_files(count, batched_requests, individual_reads):
    provider, gl, project = _diff_provider(count)

    files = provider.get_diff_files()

    assert len(files) == count
    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert files[0].head_file == "head-sha: src/file0.py\n"
    assert gl.http_request.call_count == batched_requests
    if batched_requests:
        assert all(call.args[1] == "https://gitlab.example.com/api/graphql"
                   and call.kwargs["timeout"] <= 5 and call.kwargs["streamed"] is True
                   for call in gl.http_request.call_args_list)
    assert project.files.get.call_count == individual_reads
    assert gl.http_get.call_count == 2
    assert gl.http_list.call_count == 1
    assert provider.get_diff_files() is files
    assert project.files.get.call_count == individual_reads


@pytest.mark.parametrize("failure", [None, "transport", "graphql", "malformed"])
def test_gitlab_diff_batching_preserves_mixed_file_contents_on_failure(failure):
    provider, gl, project = _diff_provider(10, mixed=True, prefetch_failure=failure)

    files = provider.get_diff_files()

    assert len(files) == 10
    for file in files:
        index = int(file.filename.removeprefix("src/file").removesuffix(".py"))
        kind = ("modified", "modified", "added", "deleted", "renamed")[index % 5]
        old_path = f"src/old{index}.py" if kind == "renamed" else file.filename
        assert file.base_file == ("" if kind == "added" else f"base-sha: {old_path}\n")
        assert file.head_file == ("" if kind == "deleted" else f"head-sha: {file.filename}\n")
    assert gl.http_request.call_count == (2 if failure is None else 1)
    assert project.files.get.call_count == (0 if failure is None else 16)


def test_gitlab_diff_batching_refetches_unverified_blob():
    provider, gl, project = _diff_provider(10, invalid_blob="src/file0.py")

    files = provider.get_diff_files()

    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert files[0].head_file == "head-sha: src/file0.py\n"
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == 2  # base and head are independently validated


def test_gitlab_diff_batching_falls_back_on_blob_digest_failure(monkeypatch):
    provider, gl, project = _diff_provider(10)
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.hashlib", SimpleNamespace(
        sha1=MagicMock(side_effect=ValueError("SHA-1 unavailable"))))

    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == 20


@pytest.mark.parametrize(("mode", "rest_reads"), [
    ("100644", 0),
    ("100755", 0),
    ("120000", 20),
    ("160000", 20),
    ("unknown", 20),
    (None, 20),
])
def test_gitlab_diff_batching_only_prefetches_regular_file_modes(mode, rest_reads):
    provider, gl, project = _diff_provider(10, blob_mode=mode)

    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == rest_reads


def test_gitlab_diff_batching_stops_at_total_timeout_budget(monkeypatch):
    provider, gl, project = _diff_provider(49)
    clock = [0.0]
    graphql = gl.http_request.side_effect

    def slow_graphql(*args, **kwargs):
        response = graphql(*args, **kwargs)
        clock[0] += 3.0
        return response

    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.monotonic", lambda: clock[0])
    gl.http_request.side_effect = slow_graphql

    files = provider.get_diff_files()

    assert len(files) == 49
    assert files[-1].base_file == "base-sha: src/file48.py\n"
    assert files[-1].head_file == "head-sha: src/file48.py\n"
    assert gl.http_request.call_count == 2
    assert [call.kwargs["timeout"] for call in gl.http_request.call_args_list] == [5.0, 2.0]
    assert all(call.kwargs["obey_rate_limit"] is False
               and call.kwargs["retry_transient_errors"] is False
               and call.kwargs["max_retries"] == 0 for call in gl.http_request.call_args_list)
    assert project.files.get.call_count == 88  # Only the first 10 base blobs are within the time budget.


def test_gitlab_diff_batching_aborts_a_slowly_streaming_response(monkeypatch):
    provider, gl, project = _diff_provider(10)
    clock = [0.0]
    graphql = gl.http_request.side_effect
    closed = MagicMock()
    reads = MagicMock()

    def slow_graphql(*args, **kwargs):
        response = graphql(*args, **kwargs)
        payload = response.raw.read1(65536, decode_content=True)
        halves = iter([payload[:len(payload) // 2], payload[len(payload) // 2:]])

        def read1(*_args, **_kwargs):
            reads()
            clock[0] += 3.0
            return next(halves)

        return SimpleNamespace(raw=SimpleNamespace(connection=response.raw.connection, read1=read1,
                                                   release_conn=MagicMock()), close=closed)

    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.monotonic", lambda: clock[0])
    gl.http_request.side_effect = slow_graphql

    files = provider.get_diff_files()

    assert len(files) == 10
    assert gl.http_request.call_count == 1
    assert reads.call_count == 2
    closed.assert_called_once_with()
    assert project.files.get.call_count == 20  # No partial JSON is used; fall back to REST.


def test_gitlab_diff_batching_shortens_read_timeout_near_deadline(monkeypatch):
    provider, gl, project = _diff_provider(10)
    clock = [0.0]
    graphql = gl.http_request.side_effect
    socket = SimpleNamespace(timeout=None)
    socket.settimeout = lambda timeout: setattr(socket, "timeout", timeout)
    reads = [0]
    close = MagicMock()

    def slow_graphql(*args, **kwargs):
        graphql(*args, **kwargs)

        def read1(*_args, **_kwargs):
            reads[0] += 1
            if reads[0] == 1:
                clock[0] = 4.9
                return b"{"
            clock[0] += socket.timeout
            raise TimeoutError("response paused before completing the JSON body")

        return SimpleNamespace(raw=SimpleNamespace(connection=SimpleNamespace(sock=socket), read1=read1,
                                                   release_conn=MagicMock()),
                               close=close)

    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.monotonic", lambda: clock[0])
    gl.http_request.side_effect = slow_graphql

    files = provider.get_diff_files()

    assert len(files) == 10
    assert gl.http_request.call_count == 1
    assert reads[0] == 2
    assert 0 < socket.timeout < 0.11
    assert clock[0] <= 5.0
    close.assert_called_once_with()
    assert project.files.get.call_count == 20


def test_gitlab_diff_batching_aborts_slow_chunk_framing(monkeypatch):
    """A trickling HTTP chunk-size line must not extend the body-read deadline."""

    class SlowChunkSizeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for char in b"00000000000010\r\n":
                try:
                    self.wfile.write(bytes((char,)))
                    self.wfile.flush()
                except OSError:
                    return
                sleep(0.12)
            try:
                self.wfile.write(b"x" * 16 + b"\r\n0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                return  # The prefetch deadline can close the client connection first.

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowChunkSizeHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    provider, gl, project = _diff_provider(10)
    url = f"http://127.0.0.1:{server.server_port}/"
    gl.http_request.side_effect = lambda *_args, **kwargs: requests.get(
        url, stream=True, timeout=kwargs["timeout"]
    )
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider._GRAPHQL_CONTENT_TIME_BUDGET", 0.4)
    try:
        started = monotonic()
        files = provider.get_diff_files()
        elapsed = monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    assert len(files) == 10
    assert elapsed < 1.6  # Framing takes ~1.9 s without an in-flight cancellation.
    assert gl.http_request.call_count == 1
    assert project.files.get.call_count == 20


def test_gitlab_diff_batching_does_not_retime_released_socket():
    provider, gl, project = _diff_provider(10)
    graphql = gl.http_request.side_effect
    released_socket = MagicMock()

    class ReleasingRaw:
        def __init__(self, payload):
            self.payload = payload
            self.connection = SimpleNamespace(sock=released_socket)
            self.reads = 0

        def release_conn(self):
            self.connection = None

        def read1(self, *_args, **_kwargs):
            self.reads += 1
            if self.reads == 1:
                self.release_conn()  # urllib3 may release the pool connection with the last data chunk.
                return self.payload
            return b""

    def release_after_last_chunk(*args, **kwargs):
        response = graphql(*args, **kwargs)
        if gl.http_request.call_count != 1:
            return response
        payload = response.raw.read1(65536, decode_content=True)
        return SimpleNamespace(raw=ReleasingRaw(payload), close=MagicMock())

    gl.http_request.side_effect = release_after_last_chunk
    files = provider.get_diff_files()

    assert len(files) == 10
    assert project.files.get.call_count == 0
    released_socket.settimeout.assert_called_once()


def test_gitlab_diff_batching_synchronizes_watchdog_and_connection_release(monkeypatch):
    provider, gl, project = _diff_provider(10)
    graphql = gl.http_request.side_effect
    timers = []
    events = []

    class ManualTimer:
        def __init__(self, _delay, callback):
            self.callback = callback

        def start(self):
            timers.append(self)

        def cancel(self):
            pass

        def join(self, timeout=None):
            assert timeout == 1.0

    class RacingRaw:
        def __init__(self, payload):
            self.payload = payload
            self.socket = MagicMock()
            self.socket.shutdown.side_effect = lambda *_args: events.append("shutdown")
            self._connection = SimpleNamespace(sock=self.socket)
            self.checked = Event()
            self.proceed = Event()
            self.reads = 0

        @property
        def connection(self):
            connection = self._connection
            if current_thread().name == "watchdog-test":
                self.checked.set()
                assert self.proceed.wait(2)
            return connection

        def release_conn(self):
            events.append("release")
            self._connection = None

        def read1(self, *_args, **_kwargs):
            self.reads += 1
            if self.reads == 1:
                return self.payload
            thread = Thread(target=timers[-1].callback, name="watchdog-test")
            thread.start()
            assert self.checked.wait(2)
            self.proceed.set()
            self.release_conn()
            thread.join(timeout=2)
            assert not thread.is_alive()
            return b""

    def simulate_release_race(*args, **kwargs):
        response = graphql(*args, **kwargs)
        if gl.http_request.call_count != 1:
            return response
        payload = response.raw.read1(65536, decode_content=True)
        return SimpleNamespace(raw=RacingRaw(payload), close=MagicMock())

    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.Timer", ManualTimer)
    gl.http_request.side_effect = simulate_release_race

    files = provider.get_diff_files()

    assert len(files) == 10
    assert project.files.get.call_count == 0
    assert events == ["shutdown", "release"]


def test_gitlab_diff_batching_falls_back_when_watchdog_cannot_start(monkeypatch):
    provider, gl, project = _diff_provider(10)
    graphql = gl.http_request.side_effect
    responses = []
    timer = MagicMock()
    timer.start.side_effect = RuntimeError("can't start new thread")
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider.Timer", lambda *_args: timer)

    def capture_response(*args, **kwargs):
        response = graphql(*args, **kwargs)
        responses.append(response)
        return response

    gl.http_request.side_effect = capture_response
    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert gl.http_request.call_count == 1
    assert project.files.get.call_count == 20
    timer.cancel.assert_called_once_with()
    timer.join.assert_not_called()
    responses[0].close.assert_called_once_with()


def test_gitlab_diff_batching_falls_back_on_oversized_response(monkeypatch):
    provider, gl, project = _diff_provider(10)
    graphql = gl.http_request.side_effect
    responses = []
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider._GRAPHQL_CONTENT_MAX_BODY_BYTES", 128)

    def capture_response(*args, **kwargs):
        response = graphql(*args, **kwargs)
        responses.append(response)
        return response

    gl.http_request.side_effect = capture_response
    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].head_file == "head-sha: src/file0.py\n"
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == 20
    for response in responses:
        response.close.assert_called_once_with()


def test_gitlab_diff_batching_keeps_later_batches_after_oversized_response(monkeypatch):
    provider, gl, project = _diff_provider(10)
    graphql = gl.http_request.side_effect
    responses = []
    monkeypatch.setattr("pr_agent.git_providers.gitlab_provider._GRAPHQL_CONTENT_MAX_BODY_BYTES", 4096)

    def oversized_base(*args, **kwargs):
        response = graphql(*args, **kwargs)
        if kwargs["post_data"]["variables"]["ref"] == "base-sha":
            response.raw.read1 = MagicMock(return_value=b"x" * 4097)
        responses.append(response)
        return response

    gl.http_request.side_effect = oversized_base
    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].base_file == "base-sha: src/file0.py\n"
    assert files[0].head_file == "head-sha: src/file0.py\n"
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == 10  # Only the oversized base batch needs REST.
    responses[0].close.assert_called_once_with()
    responses[1].close.assert_not_called()


def test_gitlab_diff_batching_keeps_strict_reads_for_missing_patches():
    provider, gl, project = _diff_provider(10, missing_patch=True)

    files = provider.get_diff_files()

    assert len(files) == 10
    assert files[0].patch
    assert gl.http_request.call_count == 2
    assert project.files.get.call_count == 2
