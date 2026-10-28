"""Test that Fitzflix refuses a database that is not MariaDB or MySQL (#500)."""

import pytest


def test_a_sqlite_uri_outside_the_tests_is_refused(app):
    from config import check_database_uri

    with pytest.raises(RuntimeError, match="must be a MariaDB or MySQL URI"):
        check_database_uri({"SQLALCHEMY_DATABASE_URI": "sqlite:///app.db"})
    with pytest.raises(RuntimeError, match="an empty setting"):
        check_database_uri({"SQLALCHEMY_DATABASE_URI": ""})


def test_mysql_and_the_test_configuration_are_accepted(app):
    from config import check_database_uri

    check_database_uri({"SQLALCHEMY_DATABASE_URI": "mysql+pymysql://u@h/fitzflix"})
    check_database_uri({"SQLALCHEMY_DATABASE_URI": "sqlite:///t.db", "TESTING": True})


def test_create_app_refuses_sqlite_without_testing(app):
    from app import create_app
    from config import Config

    class SqliteConfig(Config):
        SECRET_KEY = "a-real-secret"
        SQLALCHEMY_DATABASE_URI = "sqlite:///never-made.db"

    with pytest.raises(RuntimeError, match="sqlite"):
        create_app(SqliteConfig)
