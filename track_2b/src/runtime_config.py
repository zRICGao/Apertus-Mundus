"""Resolve configuration layers without letting a low-priority alias win."""
import os
from pathlib import Path

from dotenv import dotenv_values


LLM_ALIASES = {
    'LLM_NAME': 'APERTUS_MODEL',
    'LLM_BASE_URL': 'APERTUS_BASE_URL',
    'LLM_API_KEY': 'APERTUS_API_KEY',
}


def normalize_layer(values):
    result = {key: value for key, value in values.items() if value is not None}
    for standard, legacy in LLM_ALIASES.items():
        if standard in result or legacy in result:
            # Membership is intentional: an empty standard key disables authentication.
            value = result[standard] if standard in result else result[legacy]
            result[standard] = result[legacy] = value
    return result


def settings_file(base_dir):
    return Path(os.environ.get('RPG_CONFIG_FILE', str(Path(base_dir) / '.env')))


def load_environment(base_dir, environ=None):
    """Process > persisted settings > project .env > application defaults.

    Supplying a mapping makes resolution testable without changing the process.
    Disable interpolation so a lower layer cannot borrow a process secret.
    """
    process = dict(os.environ if environ is None else environ)
    project_file = Path(base_dir) / '.env'
    persistent_file = Path(process.get('RPG_CONFIG_FILE', str(project_file)))
    merged = {}
    paths = [project_file, persistent_file]
    # The source-tree desktop launcher lives two levels below the development
    # .env. Packaged/container installs keep their existing configuration paths.
    base = Path(base_dir).resolve()
    if (base.name == 'src' and base.parent.name == 'track_2b'
            and (base.parent.parent / 'main.py').is_file()):
        paths.insert(0, base.parent.parent / '.env')
    for path in paths:
        if path.is_file():
            merged.update(normalize_layer(dotenv_values(path, interpolate=False)))
    merged.update(normalize_layer(process))
    if environ is None:
        os.environ.update(merged)
    return merged


def validate_container_settings(settings):
    from urllib.parse import urlsplit
    if not settings.model.strip():
        raise ValueError('Set LLM_NAME (or APERTUS_MODEL) before make run.')
    try:
        address = urlsplit(settings.base_url)
        valid = (address.scheme in {'http', 'https'} and bool(address.hostname)
                 and not address.username and not address.password
                 and not address.query and not address.fragment)
        address.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Set LLM_BASE_URL to a complete HTTP(S) inference URL, usually ending in /v1.')
