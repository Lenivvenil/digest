"""Coverage vocabulary shared by evidence investigation and supplement delivery."""

COVERAGE = (
    "Limited coverage: at most one narrative from sanitized RSS excerpts, three queries, "
    "and the configured Hacker News/arXiv/Lobsters sources. "
    "Search snippets and complete arXiv abstracts are not full articles; "
    "absence of a counter-signal is not confirmation of the narrative."
)
FULL_SOURCE_COVERAGE = (
    "Limited coverage: at most one narrative from selected literal full-source passages, three queries, "
    "and the configured Hacker News/arXiv/Lobsters sources. Passage selection is model-generated, "
    "not independent corroboration or complete article coverage. "
    "Search snippets and complete arXiv abstracts are not full articles; "
    "absence of a counter-signal is not confirmation of the narrative."
)
