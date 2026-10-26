"""Pin the removal of the parameters that no task read (Oct 25 2026).

upload_task took a key prefix, 2 upload flags and a storage class. Its
body read none of them: archive_library_file decides the prefix and the
class. aws_restore took days, and every caller used the default.
rename_untouched_object took defer_upload, which #299 made dead. The
function itself went with #369.
localization_task carried force_upload and ignore_etag through 8
self-requeues and never read them."""

import inspect


def _params(function):
    return list(inspect.signature(function).parameters)


def test_the_task_signatures_carry_only_live_parameters():
    from app import aws_storage, importing

    assert _params(aws_storage.upload_task) == ["file_id"]
    assert _params(aws_storage.aws_restore) == ["key", "tier", "record_request"]
    assert not hasattr(aws_storage, "rename_untouched_object")
    assert _params(importing.localization_task) == [
        "file_path",
        "transient_retries",
        "completeness_retries",
        "space_waits",
        "tmdb_retries",
    ]


def test_every_upload_task_producer_passes_the_file_id_only():
    """Test that each enqueue_task of upload_task passes 1 argument.

    The other keywords of a producer must be options of enqueue_task,
    not task arguments."""

    import ast
    import glob

    options = {"description", "at_front", "queue", "timeout", "job_id", "meta"}
    producers = 0
    for path in glob.glob("app/**/*.py", recursive=True):
        with open(path) as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "enqueue_task"
                and node.args
                and getattr(node.args[0], "value", None) == "app.videos.upload_task"
            ):
                continue
            producers += 1
            assert len(node.args) == 2, (path, node.lineno)
            assert {k.arg for k in node.keywords} <= options, (path, node.lineno)
    assert producers >= 5


def test_the_import_requeues_carry_no_dead_flags():
    from app import importing

    source = inspect.getsource(importing)
    assert "force_upload" not in source
    assert "ignore_etag" not in source
