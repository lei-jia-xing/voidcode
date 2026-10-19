"""Agent profile plugin: manifest model, discovery, prompt materialization.

Ported from ``voidcode.agent.models``, ``voidcode.agent.registry``,
``voidcode.agent.builtin``, ``voidcode.agent.prompts`` and the framing subset
of ``voidcode.common.frontmatter``. Builtin prompt bodies live in
``profiles/<name>.txt`` (copied from ``voidcode/agent/<name>/base.txt``).
"""

from .agent_plugin import create_plugin

__all__ = ["create_plugin"]
