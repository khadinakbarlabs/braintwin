"""Current configuration names with compatibility for previously installed clients."""
import os
from collections.abc import Mapping


def env_value(name: str, env: Mapping[str, str] | None = None) -> str:
    settings = os.environ if env is None else env
    if name in settings:
        return settings[name]
    return settings.get(name.replace('BRAINTWIN_', 'SECOND_BRAIN_', 1), '')
