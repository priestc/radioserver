from __future__ import annotations

import configparser
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from django.conf import settings
from django.utils import timezone


CONF_KEYS = {
    "openai": ("openai_key", "OPENAI_API_KEY"),
    "claude": ("anthropic_key", "ANTHROPIC_API_KEY"),
    "google": ("google_ai_key", "GOOGLE_AI_API_KEY"),
    "deepseek": ("deepseek_key", "DEEPSEEK_API_KEY"),
    "groq": ("groq_key", "GROQ_API_KEY"),
    "openrouter": ("openrouter_key", "OPENROUTER_API_KEY"),
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
    "openrouter": "OpenRouter",
}


# Prompts go through the service delegator (github.com/priestc/service-delegator),
# which holds the API keys and logs every call with its timing. Its "song-year" task sets which providers
# are used, in which order (who proposes a year first) and with which model;
# radioserver still decides who checks whom.
AI_TASK = "song-year"

# Used when the delegator's task doesn't name a model for a provider
DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "claude": "claude-haiku-4-5-20251001",
    "google": "gemini-2.0-flash",
    "deepseek": "deepseek-flash",
    "groq": "openai/gpt-oss-120b",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
}
# gpt-oss on Groq is a reasoning model: its reasoning counts against
# max_tokens, so leave room for the answer. The others answer in a word.
MAX_TOKENS = {"groq": 400}


class DelegatorError(RuntimeError):
    pass


def _delegator(path: str, body: dict | None = None, timeout: float = 10) -> dict:
    url = settings.SERVICE_DELEGATOR_URL.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except ValueError:
            raise DelegatorError(f"Service delegator returned HTTP {e.code}")
        # With one provider asked, its own error is the useful part
        notes = err.get("notes") or []
        raise DelegatorError(notes[0].split(": ", 1)[-1] if len(notes) == 1 else
                             (err.get("error") or f"HTTP {e.code}") + (f" ({'; '.join(notes)})" if notes else ""))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if "timed out" in str(getattr(e, "reason", e)):
            raise DelegatorError(f"no answer within {timeout:.0f}s (timed out)")
        raise DelegatorError(f"Could not reach the service delegator at {settings.SERVICE_DELEGATOR_URL}: "
                             f"{getattr(e, 'reason', e)}")


def task_routes() -> list[dict]:
    """The song-year task's providers in order: [{provider, model}]."""
    for task in _delegator("/api/tasks")["tasks"]:
        if task["name"] == AI_TASK:
            return task["routes"]
    raise DelegatorError(f"The service delegator has no {AI_TASK!r} task; add it on its AI tasks page")


def _asker(name: str, model: str):
    def ask(prompt: str) -> str:
        answer = _delegator("/api/ask", {
            "task": AI_TASK, "project": "radioserver", "prompt": prompt, "provider": name, "model": model,
            "max_tokens": MAX_TOKENS.get(name, 20), "timeout": REQUEST_TIMEOUT,
        }, timeout=REQUEST_TIMEOUT * (REQUEST_RETRIES + 1) + 5)
        return answer["text"].strip()
    return ask


def _model_for(name: str) -> str:
    for r in task_routes():
        if r["provider"] == name:
            return r.get("model") or DEFAULT_MODELS[name]
    return DEFAULT_MODELS[name]



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
    if name not in DEFAULT_MODELS:
        raise ValueError(f"Unknown backend: {name}")
    _check_cooloff(name)
    try:
        return _asker(name, _model_for(name))
    except DelegatorError as e:
        raise ValueError(str(e))


def get_available_backends() -> list[str]:
    """Backends in the service delegator's song-year task, in its order, that aren't
    disabled here. Raises DelegatorError if the delegator can't be reached."""
    from library.models import AIServiceManager

    disabled = set(
        AIServiceManager.objects.filter(enabled=False).values_list("name", flat=True)
    )
    return [r["provider"] for r in task_routes()
            if r["provider"] in DEFAULT_MODELS and r["provider"] not in disabled]


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
    title: str, artist: str, preferred_backend: str = "", budget: float = LOOKUP_BUDGET,
) -> tuple[int | None, str, list[str]]:
    """Find a track's release year, with each answer checked by a second AI service.

    1. One backend proposes a year.
    2. A different backend is asked whether that year is right. If it agrees, done.
    3. If it disagrees, the next backend proposes a year, which is checked the same
       way, and so on through the available backends.

    Returns (year_or_none, description of who proposed/confirmed it, errors/notes).
    Only a confirmed year is returned: one no other backend could check gives None.
    """
    try:
        available = get_available_backends()
    except DelegatorError as e:
        return None, "", [str(e)]
    if not available:
        return None, "", [f"No AI backends in the service delegator's {AI_TASK!r} task (or all are disabled here)"]
    if preferred_backend in available:
        available.remove(preferred_backend)
        available.insert(0, preferred_backend)

    errors = []
    started = time.monotonic()
    out_of_time = lambda: time.monotonic() - started > budget - REQUEST_TIMEOUT

    def call(name, kind, fn):
        """Run fn(ask) against one backend; returns (ok, value). Failures are noted and cool the backend off.
        (How long each one took is logged by the service delegator.)"""
        try:
            ask = get_backend(name)
        except ValueError as e:
            errors.append(f"{name}: {e}")
            return False, None
        try:
            value = fn(ask)
        except Exception as e:
            msg = _describe_error(e)
            _log_rate_limit_error(name, msg)   # puts the backend in a 5-minute cool-off
            errors.append(f"{name}: {msg}")
            return False, None
        return True, value

    for i, proposer in enumerate(available):
        if out_of_time():
            errors.append(f"{proposer}: skipped, out of time ({LOOKUP_BUDGET}s per lookup)")
            continue
        ok, year = call(proposer, "propose", lambda ask: lookup_year(ask, title, artist, backend_name=proposer))
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
            ok, answer = call(checker, "verify", lambda ask: verify_year(ask, title, artist, year))
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
