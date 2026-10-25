"""Test the dispatch of the Profile forms and their shared sub-steps."""

from tests.conftest import page_csrf_token


def test_a_refused_field_of_any_profile_form_gets_a_message(app, admin_client):
    """The field-error flash covers every form of the page, not only 5."""

    token = page_csrf_token(admin_client)
    response = admin_client.post(
        "/profile",
        data={
            "csrf_token": token,
            "default_player": "betamax",
            "default_player_submit": "Save Default Player",
        },
    )
    assert response.status_code == 200
    assert "Not a valid choice" in response.get_data(as_text=True)


def test_username_free_ignores_case_and_the_own_account(app, admin_client):
    from app import db
    from app.main.account import _username_free
    from app.models import User

    with app.app_context():
        admin = User.query.filter_by(admin=True).first()
        member = User.query.filter_by(admin=False).first()
        member.plex_username = "Glenn"
        admin.plex_username = "Owner"
        db.session.commit()
        try:
            with app.test_request_context():
                from flask_login import login_user

                login_user(admin)
                assert _username_free(User.plex_username, "") is True
                assert _username_free(User.plex_username, None) is True
                assert _username_free(User.plex_username, "glenn") is False
                assert _username_free(User.plex_username, "OWNER") is True
                assert _username_free(User.plex_username, "someone") is True
        finally:
            member.plex_username = None
            admin.plex_username = None
            db.session.commit()


def test_player_from_address(app):
    from app.main.account import _player_from_address

    assert _player_from_address("  ") == (True, None)
    assert _player_from_address(None) == (True, None)
    assert _player_from_address("living-room.local") == (False, None)
    blank, address = _player_from_address("192.168.1.20")
    assert not blank and address.startswith("192.168.1.20:")
    assert _player_from_address("192.168.1.20", default_port=49153) == (
        False,
        "192.168.1.20:49153",
    )
