from shared.paths import RuntimePaths
from .Service import ServiceKey

APP_PATHS = ServiceKey[RuntimePaths]("application.paths")

__all__ = ["RuntimePaths", "APP_PATHS"]
