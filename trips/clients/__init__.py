"""Third-party HTTP clients. Every call goes through `http_client()` so it always has a timeout."""

import httpx
from django.conf import settings


def http_client() -> httpx.Client:
    return httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS)
