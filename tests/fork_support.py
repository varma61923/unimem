"""Account for the expected CPython warning in deliberate fork-safety probes."""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
_EXPECTED = re.compile(
    r"This process \(pid=\d+\) is multi-threaded, use of fork\(\) may lead to deadlocks in the child\."
)


def intentional_fork(call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run an intentional fork probe; keep every unrelated warning visible.

    These tests deliberately fork initialized or locked state to validate child
    recovery. Python 3.12+ warns when the test runner already owns threads; older
    interpreters and single-threaded parents need not emit that warning.
    """
    recorded: list[warnings.WarningMessage] = []
    try:
        with warnings.catch_warnings(record=True) as recorded:
            warnings.simplefilter("always", DeprecationWarning)
            result = call(*args, **kwargs)
    finally:
        for item in recorded:
            if not (issubclass(item.category, DeprecationWarning) and _EXPECTED.fullmatch(str(item.message))):
                warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)
    return result
