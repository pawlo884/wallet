"""Obserwowanie bota: każda wiadomość, przycisk, komenda i wywołanie modelu trafia do dziennika
(tabela events w data/assistant.db). Zdarzenia jednej wiadomości łączy wspólny `trace`."""

import contextvars
import secrets

# USD za 1 mln tokenów (wejście, wyjście). Zapis do cache ×1,25, odczyt z cache ×0,1 ceny wejścia.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
}
FALLBACK_PRICE = (5.0, 25.0)  # nieznany model: liczymy drożej, żeby limit nie przepuścił kosztów

trace_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace", default=None)
# Który agent obsługuje bieżącą wiadomość (dziś: wydatki / zadania / ksiazki; w etapie 3 wybierze dyrygent).
agent_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent", default=None)


def new_trace() -> str:
    t = secrets.token_hex(4)
    trace_var.set(t)
    agent_var.set(None)
    return t


def cost_usd(model: str, usage) -> float:
    price_in, price_out = PRICES.get(model, FALLBACK_PRICE)
    fresh = getattr(usage, "input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    return (fresh * price_in + written * price_in * 1.25 + read * price_in * 0.1 + out * price_out) / 1_000_000


def tokens_in(usage) -> int:
    return sum(getattr(usage, k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
