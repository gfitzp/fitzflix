"""Test that credentials never reach the log.

The SecretRedactor filter removes query-string keys by name. It also
removes the configured secret values wherever they appear. This
includes the rendered traceback of a record."""

import logging

from app.redaction import REDACTED, SecretRedactor, secret_values


def test_secret_values_come_from_credential_settings_and_the_db_uri():
    secrets = secret_values(
        {
            "TMDB_API_KEY": "tmdbkey0123456789",
            "PLEX_TOKEN": "plextoken-abcdef",
            "MAIL_PASSWORD": "hunter22",
            "SECRET_KEY": "fitzflix-secret",
            "SQLALCHEMY_DATABASE_URI": "mysql+pymysql://fitz:dbp4ssw0rd@db/fitzflix",
            "LOG_FILE": "/var/log/fitzflix.log",
            "TMDB_API_URL": "https://api.themoviedb.org/3",
            "RADARR_API_KEY": None,
            "SHORT_KEY": "abc",
        }
    )
    assert set(secrets) == {
        "tmdbkey0123456789",
        "plextoken-abcdef",
        "hunter22",
        "fitzflix-secret",
        "dbp4ssw0rd",
    }
    assert secrets == sorted(secrets, key=len, reverse=True)


def test_redactor_blanks_query_keys_and_known_values():
    redactor = SecretRedactor({"TMDB_API_KEY": "tmdbkey0123456789"})
    text = (
        "404 Client Error for url: https://api.themoviedb.org/3/movie/1"
        "?api_key=tmdbkey0123456789&language=en "
        "and https://plex/library?X-Plex-Token=abc123xyz&foo=1 "
        "and the bare key tmdbkey0123456789 and Sonarr apikey=sonarr-secret"
    )
    redacted = redactor.redact(text)
    assert "tmdbkey0123456789" not in redacted
    assert "abc123xyz" not in redacted
    assert "sonarr-secret" not in redacted
    assert f"api_key={REDACTED}&language=en" in redacted
    assert f"X-Plex-Token={REDACTED}&foo=1" in redacted
    assert f"the bare key {REDACTED}" in redacted


def test_redactor_blanks_a_cloudfront_signature():
    """Test that a CloudFront signed URL loses its Signature parameter.

    A transport error from requests names the full URL, with the query.
    Without the signature, the URL can fetch nothing."""

    redactor = SecretRedactor({})
    text = (
        "Max retries exceeded with url: /untouched/x.mkv?Expires=1788366310"
        "&Signature=QiX88rSfoOePSop-RYLjplbfJEVJi3XB8fuoupW1vjqgKAK05~V36bk"
        "&Key-Pair-Id=KTEST (Caused by NameResolutionError)"
    )
    redacted = redactor.redact(text)
    assert "QiX88rSfoOePSop" not in redacted
    assert f"&Signature={REDACTED}&Key-Pair-Id=KTEST" in redacted


def test_filter_scrubs_message_args_and_traceback(caplog):
    logger = logging.getLogger("test_redaction")
    logger.addFilter(SecretRedactor({"PLEX_TOKEN": "plextoken-abcdef"}))
    with caplog.at_level(logging.INFO, logger="test_redaction"):
        logger.info("token in args: %s", "plextoken-abcdef")
        try:
            raise RuntimeError("GET https://plex/?X-Plex-Token=plextoken-abcdef")
        except RuntimeError:
            logger.exception("request failed")

    assert "plextoken-abcdef" not in caplog.text
    assert f"token in args: {REDACTED}" in caplog.text
    assert f"X-Plex-Token={REDACTED}" in caplog.text


def test_app_logger_carries_the_redactor(app, caplog):
    """Test that the filter is installed in every mode.

    Every process logs through app.logger. Thus, Fitzflix redacts a URL
    that the app logs from any location."""

    assert any(isinstance(f, SecretRedactor) for f in app.logger.filters)
    with caplog.at_level(logging.WARNING):
        app.logger.warning(
            "https://api.themoviedb.org/3/tv/456?api_key=live-key-value&x=1"
        )
    assert "live-key-value" not in caplog.text
    assert f"api_key={REDACTED}&x=1" in caplog.text


def test_path_valued_settings_are_not_secrets():
    """Test that a setting that names a file keeps its path in logs.

    CDN_PRIVATE_KEY ends in KEY, but its value is a path. A blanked path
    would hide which file is missing."""

    secrets = secret_values(
        {"CDN_PRIVATE_KEY": "/etc/fitzflix/cdn-key.pem", "TMDB_API_KEY": "abcdef123456"}
    )
    assert secrets == ["abcdef123456"]
