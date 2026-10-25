"""Pin the removal of the parameters that no task read (Oct 25 2026).

upload_task took a key prefix, 2 upload flags and a storage class. Its
body read none of them: archive_library_file decides the prefix and the
class. aws_restore took days, and every caller used the default.
rename_untouched_object took defer_upload, which #299 made dead.
localization_task carried force_upload and ignore_etag through 8
self-requeues and never read them."""

import inspect
import re


def _params(function):
    return list(inspect.signature(function).parameters)


def test_the_task_signatures_carry_only_live_parameters():
    from app import aws_storage, importing

    assert _params(aws_storage.upload_task) == ["file_id"]
    assert _params(aws_storage.aws_restore) == ["key", "tier", "record_request"]
    assert _params(aws_storage.rename_untouched_object) == ["file", "new_key"]
    assert _params(importing.localization_task) == [
        "file_path",
        "transient_retries",
        "completeness_retries",
        "space_waits",
        "tmdb_retries",
    ]


def test_every_upload_task_producer_passes_the_file_id_only():
    import glob

    enqueue = re.compile(r'"app\.videos\.upload_task",\s*args=\(([^)]*)\)', re.S)
    producers = 0
    for path in glob.glob("app/**/*.py", recursive=True):
        with open(path) as handle:
            for match in enqueue.finditer(handle.read()):
                producers += 1
                args = [
                    part.strip() for part in match.group(1).split(",") if part.strip()
                ]
                assert len(args) == 1, (path, match.group(0))
    assert producers >= 5


def test_the_import_requeues_carry_no_dead_flags():
    from app import importing

    source = inspect.getsource(importing)
    assert "force_upload" not in source
    assert "ignore_etag" not in source
