"""Gemini-powered chat over the lineage tools (Gemini API or Vertex AI via google-genai).

Auth (standard google-genai env vars):
  * Vertex AI (typical inside a company): GOOGLE_GENAI_USE_VERTEXAI=true, GOOGLE_CLOUD_PROJECT,
    GOOGLE_CLOUD_LOCATION, plus `gcloud auth application-default login`.
  * Gemini API: GOOGLE_API_KEY (or GEMINI_API_KEY).
Model: CODELINEAGE_MODEL (default below).
"""
from __future__ import annotations

import os

from .tools import SYSTEM_PROMPT, LineageService

DEFAULT_MODEL = "gemini-2.5-flash"


class LineageChat:
    def __init__(self, service: LineageService, model: str | None = None):
        from google import genai
        from google.genai import types

        self.client = genai.Client()
        self.model = model or os.environ.get("CODELINEAGE_MODEL", DEFAULT_MODEL)
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=service.tool_functions(),
            temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=15),
        )
        self.chat = self.client.chats.create(model=self.model, config=self.config)

    def ask(self, question: str) -> str:
        return self.chat.send_message(question).text or "(no answer)"
