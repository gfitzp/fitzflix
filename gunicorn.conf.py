"""Settings of the gunicorn web process.

gunicorn reads this file from the project directory at startup. The
command line in fitzflix_supervisor.ini gives the bind address and the
worker counts. This file adds the access log. See app.access_log.

gunicorn takes every module-level name that is also a setting name.
Thus, the helpers stay behind the module name. A bare "logconfig"
import would be read as the logconfig setting.
"""

import app.access_log as access_log_settings
from config import Config

access_log_format = access_log_settings.ACCESS_LOG_FORMAT
logconfig_dict = access_log_settings.logconfig(
    access_log_settings.access_log_path(Config.LOG_FILE)
)
