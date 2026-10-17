"""Gemini-powered chat over the lineage tools (Gemini API or Vertex AI via google-genai).

Auth (standard google-genai env vars):
  * Vertex AI (typical inside a company): GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT,
    GOOGLE_CLOUD_LOCATION, plus `gcloud auth application-default login`.
  * Gemini API: GOOGLE_API_KEY (or GEMINI_API_KEY).
Model: CODELINEAGE_MODEL (default below).
"""
from __future__ import annotations

import os
import time
import uuid

from .telemetry import InteractionLogger, ToolRecorder, traced_columns
from .tools import SYSTEM_PROMPT, LineageService

DEFAULT_MODEL = "gemini-2.5-flash"


class LineageChat:
    def __init__(self, service: LineageService, model: str | None = None, *,
                 logger: InteractionLogger | None = None, surface: str = "cli",
                 user_id: str | None = None, session_id: str | None = None, client=None):
        from google.genai import types

        if client is None:
            from google import genai
            client = genai.Client()
        self.client = client
        self.model = model or os.environ.get("CODELINEAGE_MODEL", DEFAULT_MODEL)
        self.logger, self.surface, self.user_id = logger, surface, user_id
        self.session_id = session_id or uuid.uuid4().hex
        self.graph_built_at = service.graph.data.get("built_at")
        self.recorder = ToolRecorder()
        self.last_interaction_id: str | None = None
        self.last_tool_calls: list[dict] = []
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[self.recorder.wrap(f) for f in service.tool_functions()],
            temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=15),
        )
        self.chat = self.client.chats.create(model=self.model, config=self.config)

    def ask(self, question: str) -> str:
        interaction_id = uuid.uuid4().hex
        self.last_interaction_id = interaction_id
        self.recorder.reset()
        start, answer, error, usage = time.monotonic(), None, None, None
        try:
            response = self.chat.send_message(question)
            usage = getattr(response, "usage_metadata", None)
            answer = response.text or "(no answer)"
            return answer
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            calls = self.last_tool_calls = self.recorder.reset()
            if self.logger:
                self.logger.log(
                    "interaction", interaction_id=interaction_id, session_id=self.session_id,
                    surface=self.surface, user_id=self.user_id, model=self.model,
                    question=question, answer=answer, tool_calls=calls,
                    traced_columns=traced_columns(calls),
                    latency_ms=int((time.monotonic() - start) * 1000),
                    prompt_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None),
                    total_tokens=getattr(usage, "total_token_count", None),
                    error=error, graph_built_at=self.graph_built_at)

    def feedback(self, interaction_id: str, rating: int, comment: str | None = None) -> None:
        if self.logger:
            self.logger.log("feedback", interaction_id=interaction_id, session_id=self.session_id,
                            surface=self.surface, user_id=self.user_id,
                            feedback_rating=rating, feedback_comment=comment)
