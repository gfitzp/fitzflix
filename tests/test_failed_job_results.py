"""Test that a failed job and its result stream expire and leave together (#478)."""

import pytest

# A registered task. The tests queue it, but no worker runs it.

TASK = "app.videos.refresh_criterion_collection_info"


def test_enqueue_task_sets_a_failure_ttl(app):
    from app.tasks import FAILURE_TTL, enqueue_task

    with app.app_context():
        job = enqueue_task(TASK)
        try:
            assert job.failure_ttl == FAILURE_TTL == 30 * 86400
        finally:
            job.delete()


@pytest.mark.parametrize("path", ["free_job_id", "forget"])
def test_a_deleted_failed_job_takes_its_result_stream(app, admin_client, path):
    from rq.registry import FailedJobRegistry
    from rq.results import Result

    from app.tasks import TASKS, enqueue_task, free_job_id, resolve_queue

    with app.app_context():
        queue = resolve_queue(TASKS[TASK].queue)
        job = enqueue_task(TASK, job_id=f"result-stream-{path}")
        # A failure as the worker records it: the job fails, the result
        # stream has the failure, and the registry has the job
        registry = FailedJobRegistry(queue=queue)
        queue.remove(job)
        with app.redis.pipeline() as pipeline:
            job.set_status("failed", pipeline=pipeline)
            Result.create_failure(job, job.failure_ttl, "Traceback", pipeline=pipeline)
            registry.add(job, ttl=job.failure_ttl, pipeline=pipeline)
            pipeline.execute()
        key = Result.get_key(job.id)
        assert app.redis.exists(key)
        # The stream expires with the failure
        assert 0 < app.redis.ttl(key) <= 30 * 86400

        if path == "free_job_id":
            assert free_job_id(TASK, job.id) is True
        else:
            from tests.conftest import csrf_token_from

            page = admin_client.get("/system").get_data(as_text=True)
            response = admin_client.post(
                "/system",
                data={
                    "csrf_token": csrf_token_from(page),
                    "failed_queue": queue.name,
                    "failed_job_id": job.id,
                    "forget_submit": "Forget",
                },
            )
            assert response.status_code == 302
        assert not app.redis.exists(key)
        assert not app.redis.exists(job.key)
