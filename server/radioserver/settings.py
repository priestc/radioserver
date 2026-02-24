from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-change-me-before-production"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "library",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "radioserver.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "radioserver.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------------------
# Music library settings
# ---------------------------------------------------------------------------

MUSIC_EXTENSIONS = {"mp3", "flac", "m4a", "aac", "ogg", "opus", "wav", "wma"}

# Load user config from ~/.radioserver.conf
import configparser as _configparser

_config = _configparser.ConfigParser()
_config.read(Path.home() / ".radioserver.conf")

MUSIC_LIBRARY_PATH = _config.get("library", "path", fallback="/path/to/your/music")

OPENAI_API_KEY = _config.get("api", "openai_key", fallback="")
ANTHROPIC_API_KEY = _config.get("api", "anthropic_key", fallback="")
GOOGLE_AI_API_KEY = _config.get("api", "google_ai_key", fallback="")
DEEPSEEK_API_KEY = _config.get("api", "deepseek_key", fallback="")
GROQ_API_KEY = _config.get("api", "groq_key", fallback="")
