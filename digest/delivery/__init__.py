"""Compatible delivery exports, loaded without coupling pure renderers to transport."""

from typing import TYPE_CHECKING, Any

from digest.domain.delivery.outcomes import ArticleDeliveryResult as ArticleDeliveryResult

if TYPE_CHECKING:
    from digest.adapters.telegram.delivery import send_article_cards as send_article_cards
    from digest.adapters.telegram.delivery import send_counter_signals as send_counter_signals
    from digest.delivery.markdown import write_digest as write_digest

__all__ = [
    "ArticleDeliveryResult",
    "send_article_cards",
    "send_counter_signals",
    "write_digest",
]


def __getattr__(name: str) -> Any:
    if name == "write_digest":
        from digest.delivery.markdown import write_digest

        return write_digest
    if name in {"send_article_cards", "send_counter_signals"}:
        from digest.adapters.telegram.delivery import send_article_cards, send_counter_signals

        return send_article_cards if name == "send_article_cards" else send_counter_signals
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
