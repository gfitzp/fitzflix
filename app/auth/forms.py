from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField
from wtforms.validators import ValidationError, DataRequired, Email, EqualTo, Length
from app.models import User


def strip_text(value):
    """Return the typed text with no space at the start or the end (#764).

    A field that was not posted holds None. The filter keeps it."""

    return value.strip() if isinstance(value, str) else value


class LoginForm(FlaskForm):
    """Sign in a user."""

    email = StringField("Email", validators=[DataRequired()], filters=[strip_text])
    password = PasswordField("Password", validators=[DataRequired()])
    remember_me = BooleanField("Remember Me")
    submit = SubmitField("Sign In")


class RegistrationForm(FlaskForm):
    """Create an account.

    This form is open only until registration is disabled."""

    # The user.email column is VARCHAR(120) (#329)

    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=120)])
    password = PasswordField("Password", validators=[DataRequired()])
    password2 = PasswordField(
        "Repeat Password", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Register")

    def validate_email(self, email):
        """Reject an email address that is already registered."""

        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError("Please use a different email address.")


class ResetPasswordRequestForm(FlaskForm):
    """Request a password-reset email.

    A space at the start or the end of the address is not an error. The
    filter removes it before the Email check (#764)."""

    email = StringField(
        "Email", validators=[DataRequired(), Email()], filters=[strip_text]
    )
    submit = SubmitField("Request Password Reset")


class ResetPasswordForm(FlaskForm):
    """Set a new password from a reset link."""

    password = PasswordField("Password", validators=[DataRequired()])
    password2 = PasswordField(
        "Repeat Password", validators=[DataRequired(), EqualTo("password")]
    )
    submit = SubmitField("Reset Password")
