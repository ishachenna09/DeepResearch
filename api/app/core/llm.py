from langchain_openai import ChatOpenAI
from langfuse.langchain import CallbackHandler      # Langfuse v5 import path

from app.core.config import settings
from app.core.observability import get_langfuse


def get_llm(temperature: float = 0.2) -> ChatOpenAI:
    """LangChain client pointed at the LiteLLM proxy (OpenAI-compatible).

    All failover (Gemini -> Groq -> Gemini-Lite) happens proxy-side; the app
    only ever knows the virtual model name 'support-chat'.
    """
    # get_langfuse() registers the process-wide Langfuse client with our keys;
    # CallbackHandler() then binds to that singleton (v5 reads it, no args).
    callbacks = [CallbackHandler()] if get_langfuse() is not None else []

    return ChatOpenAI(
        model=settings.LLM_PRIMARY_MODEL,
        base_url=f"{settings.LITELLM_BASE_URL}/v1",
        api_key=settings.LITELLM_MASTER_KEY,
        temperature=temperature,
        timeout=90,
        max_retries=0,   # proxy already retries/fails over; don't multiply retries
        callbacks=callbacks,
    )
