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

# A backend that stalls must fail fast so the next one gets a turn, and a whole
# lookup must finish well inside nginx's 60s proxy timeout (otherwise the admin
# page only sees "504 Gateway Time-out").
REQUEST_TIMEOUT = 12   # seconds per API request
REQUEST_RETRIES = 1    # client-level retries per request
LOOKUP_BUDGET = 45     # seconds for one track's lookup across all backends

DISPLAY_NAMES = {
    "openai": "OpenAI",
    "claude": "Claude",
    "google": "Google Gemini",
    "deepseek": "DeepSeek",
    "groq": "Groq",
}


def _ask_openai(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=REQUEST_TIMEOUT, max_retries=REQUEST_RETRIES)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()


def _ask_claude(prompt: str) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=REQUEST_TIMEOUT, max_retries=REQUEST_RETRIES)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=20,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _ask_deepseek(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com",
                    timeout=REQUEST_TIMEOUT, max_retries=REQUEST_RETRIES)
    response = client.chat.completions.create(
        model="deepseek-flash",  # deepseek-chat is no longer offered
        messages=[{"role": "user", "content": prompt}],
        max_tokens=20,
        # deepseek-flash reasons before answering by default, which uses up max_tokens
        # and returns an empty reply; a one-word answer doesn't need it.
        extra_body={"thinking": {"type": "disabled"}},
    )
    return response.choices[0].message.content.strip()


def _ask_groq(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
                    timeout=REQUEST_TIMEOUT, max_retries=REQUEST_RETRIES)
    # llama-3.3-70b-versatile was retired by Groq. gpt-oss is a reasoning model:
    # its reasoning counts against max_tokens, so leave room for the answer.
    response = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400,
        reasoning_effort="low",
    )
    return response.choices[0].message.content.strip()


def _ask_google(prompt: str) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.GOOGLE_AI_API_KEY,
                          http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT * 1000))
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
    """Raise ValueError if this backend errored (rate limit, timeout, ...) in the last 5 minutes."""
    from library.models import AIServiceError, AIServiceManager

    try:
        service = AIServiceManager.objects.get(name=name)
    except AIServiceManager.DoesNotExist:
        return
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(minutes=5)
    recent = service.errors.filter(created_at__gte=cutoff).order_by("-created_at").first()
    if recent:
        raise ValueError(
            f"skipped for a 5-minute cool-off after an error at "
            f"{timezone.localtime(recent.created_at):%H:%M}: {recent.error_message[:120]}"
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
    """Return list of backend names that have API keys configured and are enabled."""
    from library.models import AIServiceManager

    disabled = set(
        AIServiceManager.objects.filter(enabled=False).values_list("name", flat=True)
    )
    available = []
    for name, (setting_name, _) in BACKENDS.items():
        if getattr(settings, setting_name, "") and name not in disabled:
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

    if not backend_name:
        return
    try:
        service, _ = AIServiceManager.objects.get_or_create(
            name=backend_name,
            defaults={"display_name": DISPLAY_NAMES.get(backend_name, backend_name)},
        )
        AIServiceError.objects.create(service=service, error_message=error_str)
    except Exception:
        pass


def lookup_year(ask, title: str, artist: str, backend_name: str = "") -> int | None:
    """Query an AI backend for a track's release year with retry logic."""
    prompt = (
        f"What year was the song '{title}' by {artist} "
        f"originally released? Reply with just the 4-digit year."
    )

    for attempt in range(2):
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
                if attempt == 0:
                    time.sleep(5)
                else:
                    raise
            else:
                raise
    return None


def verify_year(ask, title: str, artist: str, year: int) -> bool | None:
    """Ask a backend whether a proposed release year is right. None if the reply is unclear."""
    answer = ask(
        f"Is it true that the song '{title}' by {artist} was first released in {year}? "
        f"Reply with just True or False."
    ).strip().lower()
    # Check "false" first so replies like "not true" or "incorrect" aren't read as agreement.
    if re.search(r"\b(false|incorrect|not true|not correct)\b|^no\b", answer):
        return False
    if re.search(r"\b(true|correct)\b|^yes\b", answer):
        return True
    return None


def _describe_error(e: Exception) -> str:
    error_str = str(e)
    if "timed out" in error_str.lower() or type(e).__name__ in ("APITimeoutError", "ReadTimeout", "TimeoutException"):
        return f"no answer within {REQUEST_TIMEOUT}s ({type(e).__name__})"
    return error_str


def lookup_year_with_fallback(
    title: str, artist: str, preferred_backend: str = "",
) -> tuple[int | None, str, list[str]]:
    """Find a track's release year, with each answer checked by a second AI service.

    1. One backend proposes a year.
    2. A different backend is asked whether that year is right. If it agrees, done.
    3. If it disagrees, the next backend proposes a year, which is checked the same
       way, and so on through the available backends.

    Returns (year_or_none, description of who proposed/confirmed it, errors/notes).
    Only a confirmed year is returned: one no other backend could check gives None.
    """
    available = get_available_backends()
    if not available:
        return None, "", ["No AI backends configured"]
    if preferred_backend in available:
        available.remove(preferred_backend)
        available.insert(0, preferred_backend)

    errors = []
    started = time.monotonic()
    out_of_time = lambda: time.monotonic() - started > LOOKUP_BUDGET - REQUEST_TIMEOUT

    def call(name, fn):
        """Run fn(ask) against one backend; returns (ok, value). Failures are noted and cool the backend off."""
        try:
            ask = get_backend(name)
        except ValueError as e:
            errors.append(f"{name}: {e}")
            return False, None
        try:
            return True, fn(ask)
        except Exception as e:
            msg = _describe_error(e)
            _log_rate_limit_error(name, msg)   # puts the backend in a 5-minute cool-off
            errors.append(f"{name}: {msg}")
            return False, None

    for i, proposer in enumerate(available):
        if out_of_time():
            errors.append(f"{proposer}: skipped, out of time ({LOOKUP_BUDGET}s per lookup)")
            continue
        ok, year = call(proposer, lambda ask: lookup_year(ask, title, artist, backend_name=proposer))
        if not ok:
            continue
        if year is None:
            errors.append(f"{proposer}: could not parse a year from its answer")
            continue
        # Check the proposal with the other backends, starting with the next one.
        verdict = None
        for checker in available[i + 1:] + available[:i]:
            if out_of_time():
                break
            ok, answer = call(checker, lambda ask: verify_year(ask, title, artist, year))
            if not ok:
                continue
            if answer is None:
                errors.append(f"{checker}: unclear answer when checking {year}")
                continue
            verdict = (checker, answer)
            break
        if verdict is None:
            errors.append(f"{proposer} said {year}, but no other AI service could check it, so it's left blank")
            return None, "", errors
        checker, agreed = verdict
        if agreed:
            return year, f"{proposer}, confirmed by {checker}", errors
        errors.append(f"{checker} says {year} (from {proposer}) is wrong")

    return None, "", errors
