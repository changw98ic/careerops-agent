from careerops.web.routes import (
    ConsoleLoginRequired,
    create_console_routers,
    install_console_login_redirect,
    install_console_web,
)
from careerops.web.security import ConsoleWebSettings

__all__ = [
    "ConsoleLoginRequired",
    "ConsoleWebSettings",
    "create_console_routers",
    "install_console_login_redirect",
    "install_console_web",
]
