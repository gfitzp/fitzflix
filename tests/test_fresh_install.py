"""Test the fresh-install nuisances of #501."""

import logging


def test_a_manual_layout_expects_only_one_worker_for_each_queue(app, monkeypatch):
    from app.operations import maintenance

    monkeypatch.setattr(maintenance, "_supervisor_status", lambda config: None)
    expected = maintenance.expected_workers(app.config)
    assert set(expected) == set(maintenance.EXPECTED_WORKERS)
    assert set(expected.values()) == {1}

    monkeypatch.setattr(
        maintenance,
        "_supervisor_status",
        lambda config: {"fitzflix:fitzflix-sql_00": ("RUNNING", 1)},
    )
    assert maintenance.expected_workers(app.config) == maintenance.EXPECTED_WORKERS


def test_check_config_skips_the_settings_read_before_the_first_migration(
    app, monkeypatch, caplog
):
    import sqlalchemy

    from app import check_config, settings

    class NoTables:
        def has_table(self, name):
            return False

    def no_read(*args, **kwargs):
        raise AssertionError("read the settings table before it exists")

    monkeypatch.setattr(sqlalchemy, "inspect", lambda engine: NoTables())
    monkeypatch.setattr(settings, "get_setting", no_read)
    monkeypatch.setitem(app.config, "AWS_BUCKET", None)
    with caplog.at_level(logging.WARNING):
        check_config(app)
    assert "Cannot read the page settings" not in caplog.text
