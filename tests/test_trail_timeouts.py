"""Test that the best-effort helpers let the rq timeout through (#376).

The production worker is a SimpleWorker. The timeout of rq is an
exception in the task process. A helper that catches it lets the job
run on with no time limit."""

import pytest


def test_a_timeout_in_a_trail_function_propagates(app):
    from rq.timeouts import JobTimeoutException

    from app.pipeline import _best_effort

    @_best_effort()
    def stamp():
        raise JobTimeoutException("the job ran out of time")

    @_best_effort()
    def interrupted():
        raise KeyboardInterrupt()

    @_best_effort()
    def broken():
        raise ValueError("a Redis fault")

    with pytest.raises(JobTimeoutException):
        stamp()
    with pytest.raises(KeyboardInterrupt):
        interrupted()
    assert broken() is None


def test_a_timeout_in_a_follow_up_step_propagates(app):
    from rq.timeouts import JobTimeoutException

    from app.importing import _follow_up

    def step():
        raise JobTimeoutException("the job ran out of time")

    def failing_step():
        raise OSError("a mount fault")

    with app.app_context():
        with pytest.raises(JobTimeoutException):
            _follow_up("Film (2020).mkv", "a step", step)
        assert _follow_up("Film (2020).mkv", "a step", failing_step) is None
