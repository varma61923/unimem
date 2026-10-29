"""Intentional fork diagnostics never hide unrelated application warnings."""

import warnings

import pytest

from tests.fork_support import intentional_fork


def test_unrelated_warnings_survive_a_failing_probe():
    def failing_probe():
        warnings.warn("probe warning", UserWarning)
        raise ValueError("probe failed")

    with pytest.warns(UserWarning, match="probe warning"):
        with pytest.raises(ValueError, match="probe failed"):
            intentional_fork(failing_probe)


def test_unrelated_warning_remains_observable():
    with pytest.warns(UserWarning, match="unexpected warning"):
        intentional_fork(warnings.warn, "unexpected warning", UserWarning)


def test_expected_fork_diagnostic_is_accounted_for():
    def fork_probe():
        warnings.warn(
            "This process (pid=123) is multi-threaded, use of fork() may lead to deadlocks in the child.",
            DeprecationWarning,
        )
        return "child recovery checked by the caller"

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        assert intentional_fork(fork_probe) == "child recovery checked by the caller"
    assert not recorded
