"""A deterministic chat model for offline demos.

Plays back a fixed sequence of AIMessages (including tool calls) so the demo runs
with zero API keys and identical output every time. Swap for a real model string
(e.g. "anthropic:claude-sonnet-4-6") in create_agent to run it live.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class ScriptedChatModel(GenericFakeChatModel):
    """GenericFakeChatModel that tolerates bind_tools (returns itself unchanged)."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedChatModel:
        return self


def scripted_finance_model() -> ScriptedChatModel:
    """The invoice-bot's scripted 'reasoning' for the demo.

    Turn 1: query the invoice database (policy: granted to invoice-bot).
    Turn 2: try to update a Salesforce account (policy: NOT granted -> denied).
    Turn 3: react to the denial the way the gateway instructs -- stop and escalate.
    """
    return ScriptedChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_query",
                            "args": {"table": "invoices", "quarter": "Q3"},
                            "id": "call_1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "salesforce_update_account",
                            "args": {"account": "ACME", "credit_limit": 50000},
                            "id": "call_2",
                        }
                    ],
                ),
                AIMessage(
                    content=(
                        "I pulled the Q3 invoice data successfully. However, my attempt to "
                        "update the ACME Salesforce account was blocked by policy, so I am "
                        "stopping there and requesting approval from a human operator "
                        "before any account changes are made."
                    ),
                ),
            ]
        )
    )
