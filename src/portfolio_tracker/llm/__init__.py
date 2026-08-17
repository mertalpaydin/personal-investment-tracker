"""The narration layer.

Nothing in this package computes a figure. Every number a model sees was
computed elsewhere, from stored data, and is recorded verbatim in `llm_run`
alongside whatever the model said about it -- so any sentence can be checked
against its inputs afterwards.

The whole tool works with this package switched off. That is tested, not
promised: every endpoint that can carry a narrative returns its deterministic
body with `narrative: null` when there is no key, no network, or a refusal.
"""
