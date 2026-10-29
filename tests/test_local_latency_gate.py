import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FAST_ARGS = ["--sizes", "50,200", "--runs", "2"]


@pytest.mark.parametrize("arguments", [
    ["--sizes", ""], ["--sizes", "0,10"], ["--sizes", "invalid"],
    ["--runs", "0"], ["--top-k", "0"], ["--max-total-ms", "nan"],
    ["--max-alpha", "inf"], ["--sizes", "100", "--max-alpha", "1"],
    ["--sizes", "100,100", "--max-alpha", "1"],
])
def test_invalid_gate_inputs_fail_before_creating_a_corpus(monkeypatch, arguments):
    from unimem.reference import measure_local_latency as benchmark

    def unexpected_compute(**kwargs):
        pytest.fail("invalid gate inputs must not start a benchmark")

    monkeypatch.setattr(benchmark, "compute", unexpected_compute)
    monkeypatch.setattr(sys, "argv", ["measure_local_latency", *arguments])
    with pytest.raises(SystemExit) as error:
        benchmark.main()
    assert error.value.code == 2


def test_unavailable_scaling_measurements_cannot_pass_the_gate(monkeypatch, capsys):
    from unimem.reference import measure_local_latency as benchmark

    monkeypatch.setattr(benchmark, "compute", lambda **kwargs: {"points": [], "alpha_total": None})
    monkeypatch.setattr(sys, "argv", ["measure_local_latency", "--max-alpha", "1", "--json"])
    assert benchmark.main() == 1
    assert "could not be estimated" in capsys.readouterr().err


class TestItRunsAsACommand:
    def test_emits_parseable_json(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "unimem.reference.measure_local_latency",
             *_FAST_ARGS, "--json"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["points"]
        assert payload["use_cache"] is True

    def test_passes_with_a_generous_ceiling(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "unimem.reference.measure_local_latency",
             *_FAST_ARGS, "--max-total-ms", "5000"],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr


class TestTheGateActuallyCatchesTheRegressionItExistsFor:
    @pytest.mark.parametrize("use_cache,expected", [(True, 0), (False, 1)])
    def test_the_uncached_path_fails_a_ceiling_the_cached_path_clears(self, use_cache, expected):
        # Gate wiring uses fixed observations; the performance job measures real
        # timings. Earlier process timings cannot set a stable later ceiling.
        script = """
import sys
from unimem.reference import measure_local_latency as benchmark
def measurements(**kwargs):
    use_cache = kwargs['use_cache']
    return {'points': [{'n_lessons': 1600, 'total_ms': 25. if use_cache else 125.}],
            'use_cache': use_cache, 'alpha_total': None}
benchmark.compute = measurements
sys.argv = ['measure_local_latency', '--json', '--max-total-ms', '50', *sys.argv[1:]]
raise SystemExit(benchmark.main())
"""
        gated = subprocess.run(
            [sys.executable, "-c", script, *([] if use_cache else ["--no-cache"])],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert gated.returncode == expected
        assert json.loads(gated.stdout)["use_cache"] is use_cache
        if not use_cache:
            assert "FAIL" in gated.stderr and "exceeds" in gated.stderr

    @pytest.mark.parametrize("use_cache", [True, False])
    def test_real_measurement_uses_the_selected_loader(self, monkeypatch, use_cache):
        from unimem.reference import measure_local_latency as benchmark

        loads = []
        original = benchmark._uncached_iter

        def observe(root):
            loads.append(root)
            return original(root)

        monkeypatch.setattr(benchmark, "_uncached_iter", observe)
        result = benchmark.compute(sizes=(5, 10), runs=1, use_cache=use_cache)
        assert bool(loads) is not use_cache
        assert result["use_cache"] is use_cache and len(result["points"]) == 2

    def test_max_alpha_flag_fails_the_process_when_exceeded(self, tmp_path):
        # A cached path can legitimately have a flat or negative fitted slope.
        # Exercise the CLI gate with known linear measurements, not noisy timing.
        script = """
import sys
from unimem.reference import measure_local_latency as benchmark
points = [{"n_lessons": 50, "total_ms": 1.0}, {"n_lessons": 200, "total_ms": 4.0}]
report = {"points": points, "alpha_total": benchmark.fit_alpha(points, "total_ms")}
benchmark.compute = lambda **kwargs: report
sys.argv = ["measure_local_latency", "--json", "--max-alpha", "0.01"]
raise SystemExit(benchmark.main())
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 1
        assert "alpha" in result.stderr
