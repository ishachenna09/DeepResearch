"""Critic agent entrypoint:  uvicorn app.a2a.critic_app:app --port 8011

Same shape as the writer service, deliberately: two agents that boot the same way
are two agents an operator can reason about. The only differences are the card,
the executor and the port.
"""
from app.a2a.cards import critic_card
from app.a2a.critic_executor import CriticExecutor
from app.a2a.server import build_agent_app
from app.core.config import settings

app = build_agent_app(
    card=critic_card(settings.A2A_CRITIC_URL),
    executor=CriticExecutor(),
    namespace="critic",
)
