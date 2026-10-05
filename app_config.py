"""Environment-backed application settings and non-secret strategy settings."""

import json
import os
import threading

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", BASE_DIR))
SETTINGS_PATH = os.path.abspath(os.environ.get("SETTINGS_PATH", os.path.join(DATA_DIR, "settings.json")))
EXAMPLE_SETTINGS_PATH = os.path.join(BASE_DIR, "settings.example.json")

# These values are environment-only: they are never persisted to settings.json
# or sent back to the browser.
ENV_SETTING_KEYS = {
    "fyers_client_id": "FYERS_CLIENT_ID",
    "fyers_secret_key": "FYERS_SECRET_KEY",
    "fyers_redirect_uri": "FYERS_REDIRECT_URI",
    "gemini_api_key": "GEMINI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "ai_provider": "AI_PROVIDER",
    "gemini_model": "GEMINI_MODEL",
    "claude_model": "CLAUDE_MODEL",
    "openai_model": "OPENAI_MODEL",
}
SECRET_SETTING_KEYS = {
    "fyers_client_id", "fyers_secret_key", "gemini_api_key", "anthropic_api_key", "openai_api_key",
}
_SETTINGS_LOCK = threading.RLock()


def env_setting_is_set(key):
    env_name = ENV_SETTING_KEYS.get(key)
    return bool(env_name and os.environ.get(env_name, "").strip())


def load_settings(path=None):
    """Read non-secret settings and overlay configured environment values."""
    with _SETTINGS_LOCK:
        settings_path = os.path.abspath(path or SETTINGS_PATH)
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except FileNotFoundError:
            with open(EXAMPLE_SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)

        # Remove any credentials saved by older releases before continuing,
        # cleaning the old JSON file as part of this migration.
        proxy = settings.get("proxy")
        has_old_secrets = any(key in settings for key in SECRET_SETTING_KEYS | {"fyers_redirect_uri"})
        has_old_secrets = has_old_secrets or (isinstance(proxy, dict) and any(k in proxy for k in ("user", "pass")))
        if has_old_secrets and os.path.isfile(settings_path):
            cleaned = dict(settings)
            for key in SECRET_SETTING_KEYS | {"fyers_redirect_uri"}:
                cleaned.pop(key, None)
            if isinstance(proxy, dict):
                cleaned["proxy"] = dict(proxy)
                cleaned["proxy"].pop("user", None)
                cleaned["proxy"].pop("pass", None)
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, indent=2)

    # Credentials from older settings.json files are deliberately ignored.
    # Deploy secrets must come from the environment, never a checked-in file.
    for key in SECRET_SETTING_KEYS | {"fyers_redirect_uri"}:
        settings.pop(key, None)
    for key, env_name in ENV_SETTING_KEYS.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            settings[key] = value
    # The headless console flow retains its local port-5000 catcher. The web
    # dashboard derives its own callback URL unless FYERS_REDIRECT_URI is set.
    settings.setdefault("fyers_redirect_uri", "http://127.0.0.1:5000/")
    proxy = settings.setdefault("proxy", {})
    proxy.pop("user", None)
    proxy.pop("pass", None)
    for key, env_name in (("user", "PROXY_USERNAME"), ("pass", "PROXY_PASSWORD")):
        value = os.environ.get(env_name, "").strip()
        if value:
            proxy[key] = value
    return settings


def save_settings(settings, path=None):
    """Persist user-editable non-secret preferences only."""
    with _SETTINGS_LOCK:
        settings_path = os.path.abspath(path or SETTINGS_PATH)
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        safe_settings = dict(settings)
        for key in SECRET_SETTING_KEYS | {"fyers_redirect_uri"}:
            safe_settings.pop(key, None)
        if isinstance(safe_settings.get("proxy"), dict):
            safe_settings["proxy"] = dict(safe_settings["proxy"])
            safe_settings["proxy"].pop("user", None)
            safe_settings["proxy"].pop("pass", None)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(safe_settings, f, indent=2)
