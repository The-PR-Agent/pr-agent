"""Exercise overflow translation in fresh single- and multiprocess clients."""

import inspect
import os
import subprocess
import sys
from textwrap import dedent

import pytest
from opentelemetry.sdk.metrics.view import View


def _run(code, state_dir=None):
    env = os.environ.copy()
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    env.pop("prometheus_multiproc_dir", None)
    if state_dir is not None:
        env["PROMETHEUS_MULTIPROC_DIR"] = str(state_dir)
    result = subprocess.run([sys.executable, "-c", dedent(code)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("multiprocess", [False, True])
def test_overflow_schema_order_and_registry(tmp_path, multiprocess):
    state_dir = tmp_path if multiprocess else None
    _run('''
        from types import SimpleNamespace
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, Sum
        from prometheus_client.parser import text_string_to_metric_families
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter, prometheus_response

        exporter = PrometheusMetricExporter()
        def point(attributes, value):
            return SimpleNamespace(attributes=attributes, value=value)
        def emit(name, points, kind):
            if kind == "counter":
                data = Sum(points, AggregationTemporality.DELTA, True)
            elif kind == "sum":
                data = Sum(points, AggregationTemporality.CUMULATIVE, False)
            else:
                data = Gauge(points)
            exporter._translate(SimpleNamespace(name=name, description="test", data=data))

        for kind in ("counter", "gauge", "sum"):
            for order in ("normal_first", "overflow_first"):
                name = f"{kind}_{order}"
                points = [point({"tag": ""}, 5), point({"otel.metric.overflow": True}, 2)]
                if order == "overflow_first":
                    points.reverse()
                emit(name, points, kind)
                emit(name, [point({"tag": "next"}, 3), point({"otel.metric.overflow": True}, 4)], kind)
        # Sanitized lookalikes and string values are normal attributes, not the SDK marker.
        emit("collision_keys", [point({"otel_metric_overflow": True, "tag": "x"}, 7),
                                point({"otel.metric.overflow": True}, 2)], "counter")
        emit("string_marker", [point({"otel.metric.overflow": "true"}, 7),
                               point({"otel.metric.overflow": True}, 2)], "counter")
        emit("string_only", [point({"otel.metric.overflow": "True"}, 7),
                             point({"tag": "x"}, 3)], "counter")
        # Registration still reserves the metric name, as before.
        try:
            PrometheusMetricExporter()._translate(SimpleNamespace(
                name="counter_normal_first", description="test",
                data=Sum([point({"tag": "x"}, 1)], AggregationTemporality.DELTA, True)))
        except ValueError:
            pass
        else:
            raise AssertionError("duplicate metric family registered")

        families = list(text_string_to_metric_families(prometheus_response().body.decode()))
        samples = {}
        for family in families:
            for sample in family.samples:
                labels = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "pid"))
                key = (sample.name, labels)
                assert key not in samples, key
                samples[key] = sample.value
        def value(name, labels):
            return samples[(name, tuple(sorted(labels.items())))]
        for kind in ("counter", "gauge", "sum"):
            for order in ("normal_first", "overflow_first"):
                name = f"{kind}_{order}" + ("_total" if kind == "counter" else "")
                assert value(name, {"tag": ""}) == 5
                assert value(name, {"tag": "next"}) == 3
                assert value(name, {"otel_metric_overflow": "true"}) == (6 if kind == "counter" else 4)
        assert value("collision_keys_total", {"pr_agent_escaped_otel_metric_overflow": "True", "tag": "x"}) == 7
        assert value("collision_keys_total", {"otel_metric_overflow": "true"}) == 2
        assert value("string_marker_total", {"pr_agent_escaped_otel_metric_overflow": "true"}) == 7
        assert value("string_marker_total", {"otel_metric_overflow": "true"}) == 2
        assert value("string_only_total", {"pr_agent_escaped_otel_metric_overflow": "True"}) == 7
        assert value("string_only_total", {"pr_agent_escaped_otel_metric_overflow": ""}) == 3
    ''', state_dir)


def test_overflow_across_workers(tmp_path):
    worker = '''
        from types import SimpleNamespace
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, Sum
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter
        points = [SimpleNamespace(attributes={"tag": ""}, value=5),
                  SimpleNamespace(attributes={"otel.metric.overflow": True}, value=2)]
        metric = SimpleNamespace(name="worker_overflow", description="test",
                                 data=Sum(points, AggregationTemporality.DELTA, True))
        PrometheusMetricExporter()._translate(metric)
        gauge = SimpleNamespace(name="worker_gauge", description="test", data=Gauge(points))
        PrometheusMetricExporter()._translate(gauge)
    '''
    _run(worker, tmp_path)
    _run(worker, tmp_path)
    body = _run('''
        from pr_agent.telemetry.prometheus import prometheus_response
        print(prometheus_response().body.decode())
    ''', tmp_path)
    assert 'worker_overflow_total{tag=""} 10.0' in body
    assert 'worker_overflow_total{otel_metric_overflow="true"} 4.0' in body
    gauge_lines = [line for line in body.splitlines() if line.startswith("worker_gauge{")]
    assert len(gauge_lines) == 4
    assert sum('tag=""' in line and line.endswith(" 5.0") for line in gauge_lines) == 2
    assert sum('otel_metric_overflow="true"' in line and line.endswith(" 2.0") for line in gauge_lines) == 2
    assert all('pid="' in line for line in gauge_lines)


@pytest.mark.skipif("aggregation_cardinality_limit" not in inspect.signature(View).parameters,
                    reason="requires OpenTelemetry SDK cardinality limits")
@pytest.mark.parametrize("multiprocess", [False, True])
def test_sdk_overflow_and_delta_collections(tmp_path, multiprocess):
    _run('''
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import View
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter, prometheus_response

        reader = PeriodicExportingMetricReader(PrometheusMetricExporter(), export_interval_millis=1000000)
        provider = MeterProvider(metric_readers=[reader],
                                 views=[View(instrument_name="sdk_overflow", aggregation_cardinality_limit=2)])
        counter = provider.get_meter("test").create_counter("sdk_overflow")
        counter.add(5, {"tag": ""})
        counter.add(1, {"tag": "a"})
        counter.add(2, {"tag": "b"})
        reader.collect()
        counter.add(3, {"tag": ""})
        counter.add(1, {"tag": "a"})
        counter.add(7, {"tag": "b"})
        reader.collect()
        # A third collection can admit an attribute set that previously overflowed.
        counter.add(11, {"tag": "b"})
        reader.collect()
        reader.collect()
        body = prometheus_response().body.decode()
        assert 'sdk_overflow_total{tag=""} 8.0' in body
        assert 'sdk_overflow_total{tag="a"} 2.0' in body
        assert 'sdk_overflow_total{otel_metric_overflow="true"} 9.0' in body
        assert 'sdk_overflow_total{tag="b"} 11.0' in body
        provider.shutdown()
    ''', tmp_path if multiprocess else None)


@pytest.mark.parametrize("multiprocess", [False, True])
def test_reserved_label_and_unlabeled_exports(tmp_path, multiprocess):
    _run('''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, MetricExportResult, Sum
        from prometheus_client.parser import text_string_to_metric_families
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter, prometheus_response

        exporter = PrometheusMetricExporter()
        def point(attributes, value):
            return NS(attributes=attributes, value=value)
        def export(name, kind, points):
            if kind == "counter":
                data = Sum(points, AggregationTemporality.DELTA, True)
            elif kind == "sum":
                data = Sum(points, AggregationTemporality.CUMULATIVE, False)
            else:
                data = Gauge(points)
            metric = NS(name=name, description="test", data=data)
            payload = NS(resource_metrics=[NS(scope_metrics=[NS(metrics=[metric])])])
            assert exporter.export(payload) == MetricExportResult.SUCCESS

        marker = {"otel.metric.overflow": True}
        for kind in ("counter", "gauge", "sum"):
            for order in ("normal_first", "overflow_first"):
                for case, normal in (("collision", {"otel_metric_overflow": True}), ("unlabeled", {})):
                    name = f"{kind}_{order}_{case}"
                    points = [point(normal, 5), point(marker, 2)]
                    if order == "overflow_first":
                        points.reverse()
                    # Separate exports exercise a collision appearing in a later collection.
                    export(name, kind, [points[0]])
                    export(name, kind, [points[1]])
                    export(name, kind, [point(normal, 3), point(marker, 4)])
        # Escape the prefix itself as well as the reserved label, with no collision.
        export("prefix", "counter", [point({"otel-metric-overflow": "a",
               "pr_agent_escaped_otel_metric_overflow": "b", "unaffected": "c",
               "pr_agent_escaped_": "d"}, 1), point(marker, 2)])
        families = list(text_string_to_metric_families(prometheus_response().body.decode()))
        samples = {}
        for family in families:
            for sample in family.samples:
                labels = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "pid"))
                key = (sample.name, labels)
                assert key not in samples, key
                samples[key] = sample.value
        def value(name, labels):
            return samples[(name, tuple(sorted(labels.items())))]
        for kind in ("counter", "gauge", "sum"):
            for order in ("normal_first", "overflow_first"):
                for case in ("collision", "unlabeled"):
                    name = f"{kind}_{order}_{case}" + ("_total" if kind == "counter" else "")
                    labels = {"pr_agent_escaped_otel_metric_overflow": "True"} if case == "collision" else {}
                    assert value(name, labels) == (8 if kind == "counter" else 3)
                    assert value(name, {"otel_metric_overflow": "true"}) == (6 if kind == "counter" else 4)
        assert value("prefix_total", {"pr_agent_escaped_otel_metric_overflow": "a",
            "pr_agent_escaped_pr_agent_escaped_otel_metric_overflow": "b", "unaffected": "c",
            "pr_agent_escaped_pr_agent_escaped_": "d"}) == 1
        assert value("prefix_total", {"otel_metric_overflow": "true"}) == 2
    ''', tmp_path if multiprocess else None)


def test_reserved_label_across_workers(tmp_path):
    worker = '''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Gauge, Sum
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter
        exporter = PrometheusMetricExporter()
        normal = NS(attributes={"otel_metric_overflow": True}, value=5)
        overflow = NS(attributes={"otel.metric.overflow": True}, value=2)
        for kind in ("counter", "gauge", "sum"):
            for point in POINTS:
                if kind == "counter":
                    data = Sum([point], AggregationTemporality.DELTA, True)
                elif kind == "sum":
                    data = Sum([point], AggregationTemporality.CUMULATIVE, False)
                else:
                    data = Gauge([point])
                exporter._translate(NS(name="worker_collision_" + kind, description="test", data=data))
    '''
    _run(worker.replace("POINTS", "(normal, overflow)"), tmp_path)
    _run(worker.replace("POINTS", "(overflow, normal)"), tmp_path)
    body = _run('''
        from pr_agent.telemetry.prometheus import prometheus_response
        print(prometheus_response().body.decode())
    ''', tmp_path)
    assert 'worker_collision_counter_total{pr_agent_escaped_otel_metric_overflow="True"} 10.0' in body
    assert 'worker_collision_counter_total{otel_metric_overflow="true"} 4.0' in body
    for kind in ("gauge", "sum"):
        lines = [line for line in body.splitlines() if line.startswith(f"worker_collision_{kind}{{")]
        assert len(lines) == 4
        assert sum('pr_agent_escaped_otel_metric_overflow="True"' in line and line.endswith(" 5.0")
                   for line in lines) == 2
        assert sum('otel_metric_overflow="true"' in line and 'pr_agent_escaped_' not in line
                   and line.endswith(" 2.0") for line in lines) == 2
        assert all('pid="' in line for line in lines)


@pytest.mark.parametrize("multiprocess", [False, True])
def test_colon_escaping_reserves_final_label_names(tmp_path, multiprocess):
    _run('''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Sum
        from prometheus_client.parser import text_string_to_metric_families
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter, prometheus_response

        exporter = PrometheusMetricExporter()
        def emit(name, attributes, value):
            point = NS(attributes=attributes, value=value)
            data = Sum([point], AggregationTemporality.DELTA, True)
            exporter._translate(NS(name=name, description="test", data=data))

        for order in ("normal_first", "overflow_first"):
            points = [({"otel:metric:overflow": True}, 5), ({"otel.metric.overflow": True}, 2)]
            if order == "overflow_first":
                points.reverse()
            for attrs, value in points:
                emit("colon_" + order, attrs, value)
        emit("colon_prefix", {"pr_agent:escaped_otel_metric_overflow": "a",
             "pr_agent_escaped_pr_agent_escaped_otel_metric_overflow": "b",
             "normal:label": "c"}, 3)
        emit("colon_prefix", {"otel.metric.overflow": True}, 2)
        samples = {}
        for family in text_string_to_metric_families(prometheus_response().body.decode()):
            for sample in family.samples:
                labels = tuple(sorted((k, v) for k, v in sample.labels.items() if k != "pid"))
                key = (sample.name, labels)
                assert key not in samples, key
                samples[key] = sample.value
        def value(name, labels):
            return samples[(name, tuple(sorted(labels.items())))]
        for order in ("normal_first", "overflow_first"):
            name = "colon_" + order + "_total"
            assert value(name, {"pr_agent_escaped_otel_metric_overflow": "True"}) == 5
            assert value(name, {"otel_metric_overflow": "true"}) == 2
        assert value("colon_prefix_total", {"pr_agent_escaped_pr_agent_escaped_otel_metric_overflow": "a",
            "pr_agent_escaped_pr_agent_escaped_pr_agent_escaped_otel_metric_overflow": "b",
            "normal_label": "c"}) == 3
        assert value("colon_prefix_total", {"otel_metric_overflow": "true"}) == 2
    ''', tmp_path if multiprocess else None)


def test_colon_and_underscore_reserved_labels_merge_across_workers(tmp_path):
    worker = '''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Sum
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter
        exporter = PrometheusMetricExporter()
        for attributes, value in [({LABEL: True}, 5), ({"otel.metric.overflow": True}, 2)]:
            point = NS(attributes=attributes, value=value)
            data = Sum([point], AggregationTemporality.DELTA, True)
            exporter._translate(NS(name="worker_colon", description="test", data=data))
    '''
    _run(worker.replace("LABEL", '"otel:metric:overflow"'), tmp_path)
    _run(worker.replace("LABEL", '"otel_metric_overflow"'), tmp_path)
    body = _run('''
        from pr_agent.telemetry.prometheus import prometheus_response
        print(prometheus_response().body.decode())
    ''', tmp_path)
    assert body.count('worker_colon_total{pr_agent_escaped_otel_metric_overflow="True"} 10.0') == 1
    assert body.count('worker_colon_total{otel_metric_overflow="true"} 4.0') == 1


def test_retained_old_colon_label_and_overflow_remain_separate(tmp_path):
    _run('''
        from prometheus_client import Counter
        Counter("retained_colon", "test", ["otel:metric:overflow"]).labels("True").inc(5)
    ''', tmp_path)
    _run('''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Sum
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter
        point = NS(attributes={"otel.metric.overflow": True}, value=2)
        data = Sum([point], AggregationTemporality.DELTA, True)
        PrometheusMetricExporter()._translate(NS(name="retained_colon", description="test", data=data))
    ''', tmp_path)
    body = _run('''
        from pr_agent.telemetry.prometheus import prometheus_response
        print(prometheus_response().body.decode())
    ''', tmp_path)
    lines = [line for line in body.splitlines() if line.startswith('retained_colon_total{')]
    assert sorted(lines) == sorted([
        'retained_colon_total{otel_metric_overflow="True"} 5.0',
        'retained_colon_total{otel_metric_overflow="true"} 2.0',
    ])


def test_retained_old_counter_and_overflow_remain_separate(tmp_path):
    pid = int(_run('''
        import os
        from prometheus_client import Counter
        Counter("retained_counter", "test", ["otel_metric_overflow"]).labels("True").inc(5)
        print(os.getpid())
    ''', tmp_path).strip())
    assert (tmp_path / f"counter_{pid}.db").exists()
    _run(f'''
        from prometheus_client.multiprocess import mark_process_dead
        mark_process_dead({pid})
    ''', tmp_path)
    assert (tmp_path / f"counter_{pid}.db").exists()
    _run('''
        from types import SimpleNamespace as NS
        from opentelemetry.sdk.metrics.export import AggregationTemporality, Sum
        from pr_agent.telemetry.prometheus import PrometheusMetricExporter
        point = NS(attributes={"otel.metric.overflow": True}, value=2)
        data = Sum([point], AggregationTemporality.DELTA, True)
        PrometheusMetricExporter()._translate(NS(name="retained_counter", description="test", data=data))
    ''', tmp_path)
    body = _run('''
        from pr_agent.telemetry.prometheus import prometheus_response
        print(prometheus_response().body.decode())
    ''', tmp_path)
    assert 'retained_counter_total{otel_metric_overflow="True"} 5.0' in body
    assert 'retained_counter_total{otel_metric_overflow="true"} 2.0' in body
