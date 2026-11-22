"""AgentCards: what each agent publishes about itself.

The card is the contract. A caller does not need to know a port, a prompt or a
payload shape before asking: it fetches `/.well-known/agent-card.json`, reads the
skills and the interfaces, and only then sends a message. That is the difference
between "a service I call" and "an agent others can discover".

Why advertise streaming in `capabilities`?
Because the writer answers in tokens, and a client that cannot read `SendStreamingMessage`
needs to know it should use `SendMessage` and wait. Claiming it when it were not
supported would make every streaming caller hang until the task completes.

Why declare the security scheme even though the token is a shared secret?
The card is how a *generic* client learns it must send a bearer token. If we
omitted it, a caller would get a bare 401 with no machine-readable explanation.
"""
from __future__ import annotations

from urllib.parse import urlparse

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from app.core.config import settings

PROTOCOL_VERSION = "1.0"


def _security() -> tuple[dict, list[SecurityRequirement]]:
    scheme = SecurityScheme(
        http_auth_security_scheme=HTTPAuthSecurityScheme(
            scheme="bearer",
            bearer_format="opaque",
            description="Shared agent token (A2A_TOKEN). Phase 10 replaces this with mTLS.",
        )
    )
    # `StringList(list=[])` = "this scheme with no scopes", per the spec.
    return {"bearer": scheme}, [SecurityRequirement(schemes={"bearer": StringList(list=[])})]


def _card(
    *,
    name: str,
    description: str,
    url: str,
    skills: list[AgentSkill],
    input_modes: list[str],
    output_modes: list[str],
) -> AgentCard:
    schemes, requirements = _security()
    return AgentCard(
        name=name,
        description=description,
        version=settings.APP_VERSION,
        provider=AgentProvider(organization="DeepResearch", url=settings.FRONTEND_URL),
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION,
            )
        ],
        security_schemes=schemes,
        security_requirements=requirements,
        default_input_modes=input_modes,
        default_output_modes=output_modes,
        skills=skills,
    )


def writer_card(url: str | None = None) -> AgentCard:
    return _card(
        name="deepresearch-writer",
        description=(
            "Writes the final research brief from a numbered source list. Streams "
            "markdown and returns token usage plus the model that actually served it."
        ),
        url=url or settings.A2A_WRITER_URL,
        input_modes=["application/json"],
        output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="write-cited-report",
                name="Write cited report",
                description=(
                    "Input: question, plan, and a numbered registry of notes. Output: a "
                    "markdown brief where every factual sentence cites [n] from that "
                    "registry. Unknown numbers are audited, not silently accepted."
                ),
                tags=["research", "writing", "citations", "markdown"],
                examples=[
                    "Write a cited brief answering this question from these 30 sources",
                ],
                input_modes=["application/json"],
                output_modes=["text/plain"],
            )
        ],
    )


def critic_card(url: str | None = None) -> AgentCard:
    return _card(
        name="deepresearch-critic",
        description=(
            "Judges whether the collected evidence can answer the question, and names "
            "at most three genuinely missing angles. Returns a verdict, not a policy."
        ),
        url=url or settings.A2A_CRITIC_URL,
        input_modes=["application/json"],
        output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="critique-coverage",
                name="Critique coverage",
                description=(
                    "Input: question, approved sub-questions, and a digest of the notes. "
                    "Output: {sufficient, gaps, reason}. The caller owns the stop policy."
                ),
                tags=["research", "evaluation", "coverage"],
                examples=[
                    "Is this evidence enough to write a balanced, cited answer?",
                ],
                input_modes=["application/json"],
                output_modes=["application/json"],
            )
        ],
    )


def rpc_path(card: AgentCard) -> str:
    """Path of the JSON-RPC endpoint named by the card's first interface."""
    url = card.supported_interfaces[0].url if card.supported_interfaces else ""
    return urlparse(url).path or "/"
