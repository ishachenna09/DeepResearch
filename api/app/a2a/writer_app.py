"""Writer agent entrypoint:  uvicorn app.a2a.writer_app:app --port 8010

One module per service, and `app` as the module-level object, so `uvicorn` can
import it exactly like the API. Nothing else in this file should grow: a service
entrypoint that contains logic is a service you cannot test without a socket.

Behind CORS: none. Agents talk to agents, not browsers - a browser that could
call these endpoints would let any page the user visits spend their LLM quota.
"""
from app.a2a.cards import writer_card
from app.a2a.server import build_agent_app
from app.a2a.writer_executor import WriterExecutor
from app.core.config import settings

app = build_agent_app(
    card=writer_card(settings.A2A_WRITER_URL),
    executor=WriterExecutor(),
    namespace="writer",
)
