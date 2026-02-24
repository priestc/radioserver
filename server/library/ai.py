from __future__ import annotations

import configparser
import re
import time
from pathlib import Path

from django.conf import settings
from django.utils import timezone


CONF_KEYS = {
    "openai": ("openai_key", "OPENAI_API_KEY"),
    "claude": ("anthropic_key", "ANTHROPIC_API_KEY"),
    "google": ("google_ai_key", "GOOGLE_AI_API_KEY"),
    "deepseek": ("deepseek_key", "DEEPSEEK_API_KEY"),
    "groq": ("groq_key", "GROQ_API_KEY"),
}

DISPLAY_NAMES = {
    "openai": "OpenAI",
    "claude": "Claude",
    "google": "Google Gemini",
    "deepseek": "DeepSeek",
    "groq": "Groq",
}


def _ask_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_claude(prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _ask_deepseek(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_groq(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_google(prompt: str) -> str:
    from google import genai

    client = genai.Client(api_key=settings.GOOGLE_AI_API_KEY)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    return response.text.strip()


BACKENDS = {
    "openai": ("OPENAI_API_KEY", _ask_openai),
    "claude": ("ANTHROPIC_API_KEY", _ask_claude),
    "google": ("GOOGLE_AI_API_KEY", _ask_google),
    "deepseek": ("DEEPSEEK_API_KEY", _ask_deepseek),
    "groq": ("GROQ_API_KEY", _ask_groq),
}


def _check_cooloff(name: str) -> None:
    """Raise ValueError if a 429 error occurred for this backend in the last 5 minutes."""
    from library.models import AIServiceError, AIServiceManager

    try:
        service = AIServiceManager.objects.get(name=name)
    except AIServiceManager.DoesNotExist:
        return
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(minutes=5)
    if service.errors.filter(created_at__gte=cutoff).exists():
        raise ValueError(
            f"{name}: call canceled due to 429 cool-off "
            f"(rate-limit error within the last 5 minutes)"
        )


def get_backend(name: str):
    """Return the ask function for the given backend name, or raise ValueError."""
    if name not in BACKENDS:
        raise ValueError(f"Unknown backend: {name}")
    setting_name, ask_fn = BACKENDS[name]
    if not getattr(settings, setting_name, ""):
        raise ValueError(
            f"{setting_name} not set. Configure it in ~/.radioserver.conf"
        )
    _check_cooloff(name)
    return ask_fn


def get_available_backends() -> list[str]:
    """Return list of backend names that have API keys configured."""
    available = []
    for name, (setting_name, _) in BACKENDS.items():
        if getattr(settings, setting_name, ""):
            available.append(name)
    return available


def save_api_key(name: str, key: str) -> None:
    """Write an API key to ~/.radioserver.conf and update settings in-memory."""
    if name not in CONF_KEYS:
        raise ValueError(f"Unknown backend: {name}")
    conf_key, settings_attr = CONF_KEYS[name]
    conf_path = Path.home() / ".radioserver.conf"
    config = configparser.ConfigParser()
    config.read(conf_path)
    if not config.has_section("api"):
        config.add_section("api")
    config.set("api", conf_key, key)
    with open(conf_path, "w") as f:
        config.write(f)
    setattr(settings, settings_attr, key)


def test_backend(name: str) -> tuple[bool, str]:
    """Test a backend with a simple prompt. Returns (success, message)."""
    try:
        ask = get_backend(name)
        answer = ask("What is 2+2? Reply with just the number.")
        return True, answer
    except Exception as e:
        return False, str(e)


def ensure_services() -> None:
    """Create AIServiceManager rows for any missing backends."""
    from library.models import AIServiceManager

    for name, display in DISPLAY_NAMES.items():
        AIServiceManager.objects.get_or_create(
            name=name, defaults={"display_name": display}
        )


def _log_rate_limit_error(backend_name: str, error_str: str) -> None:
    """Log a 429/rate-limit error to AIServiceError."""
    from library.models import AIServiceError, AIServiceManager

    try:
        service = AIServiceManager.objects.get(name=backend_name)
        AIServiceError.objects.create(service=service, error_message=error_str)
    except AIServiceManager.DoesNotExist:
        pass


def lookup_year(ask, title: str, artist: str, backend_name: str = "") -> int | None:
    """Query an AI backend for a track's release year with retry logic."""
    prompt = (
        f"What year was the song '{title}' by {artist} "
        f"originally released? Reply with just the 4-digit year."
    )

    for attempt in range(5):
        try:
            answer = ask(prompt)
            match = re.search(r"\b(19\d{2}|20\d{2})\b", answer)
            if match:
                return int(match.group(1))
            return None
        except Exception as e:
            error_str = str(e)
            if "insufficient_quota" in error_str:
                raise
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                _log_rate_limit_error(backend_name, error_str)
                time.sleep(10 * (attempt + 1))
            else:
                raise
    return None
