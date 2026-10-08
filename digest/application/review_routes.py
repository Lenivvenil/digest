"""Shared approved routes for bounded trial, resume and post-delivery work."""

ALLOWED_REVIEW_MODELS = {
    ("gemini", "gemini-3.8-flash"),
    ("groq", "openai/gpt-oss-120b"),
    ("groq", "qwen/qwen3.8-27b"),
}
