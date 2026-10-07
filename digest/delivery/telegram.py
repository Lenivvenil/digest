"""Compatible Telegram rendering, delivery and outcome imports.

Presentation and each concrete transport protocol have separate owners. Private
aliases retain imports; tests and internal callers patch the actual owner.
"""

from digest.adapters.telegram.delivery import _MAX_RETRIES as _MAX_RETRIES
from digest.adapters.telegram.delivery import _send_chunk as _send_chunk
from digest.adapters.telegram.delivery import send_article_cards as send_article_cards
from digest.adapters.telegram.delivery import send_compact_issue as send_compact_issue
from digest.adapters.telegram.delivery import send_counter_signals as send_counter_signals
from digest.adapters.telegram.delivery import send_status_message as send_status_message
from digest.domain.delivery.outcomes import ArticleDeliveryResult as ArticleDeliveryResult
from digest.domain.delivery.outcomes import IssueDeliveryResult as IssueDeliveryResult
from digest.presentation.telegram import escape_markdownv2 as escape_markdownv2
from digest.presentation.telegram import render_compact_issue
from digest.presentation.telegram import split_message as split_message
from digest.presentation.telegram import to_markdownv2 as to_markdownv2

_render_compact_issue = render_compact_issue

__all__ = [
    "ArticleDeliveryResult", "IssueDeliveryResult", "escape_markdownv2", "to_markdownv2", "split_message",
    "send_article_cards", "send_compact_issue", "send_counter_signals", "send_status_message",
]
