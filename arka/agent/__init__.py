"""Agentic intake and orchestration.

The language model parses input, chooses which deterministic function to call,
retrieves current regulation and writes prose. It never produces a number.
`tools.py` holds the functions it is allowed to call; `client.py` holds the
guard that stops a model-invented figure reaching the screen.
"""

__all__ = ["client", "schema", "tools"]
