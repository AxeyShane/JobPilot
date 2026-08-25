"""
Unified LLM client for JobPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-2.0-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds -- fine for cloud providers (Gemini/OpenAI)

# Local models (LLM_URL) generate far slower than cloud APIs on consumer
# hardware -- e.g. ~10-15 tok/s is common, and tailor/cover calls request up
# to 2048-4096 tokens, which alone can exceed 120s before any system load.
# Generous but still bounded so a truly dead local endpoint doesn't hang forever.
_LOCAL_TIMEOUT = 600  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str, timeout: int = _TIMEOUT) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        # Genuinely-local llama.cpp/Ollama-style endpoint, as opposed to a
        # real cloud API (OpenRouter, OpenAI, ...) that just happens to also
        # be OpenAI-compatible and non-Gemini. Matters for chat_template_kwargs
        # below -- a llama.cpp-only param that a strict cloud gateway could
        # reject outright rather than silently ignore.
        self._is_local: bool = "127.0.0.1" in base_url or "localhost" in base_url

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        extraction_schema: dict | None = None,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # Hybrid-thinking local models (Qwen3+, SmolLM3, ...) burn the whole
        # token budget on a hidden reasoning_content field unless explicitly
        # told not to via this template kwarg -- confirmed empty `content`
        # otherwise. Sent unconditionally for any *local* request rather than
        # gated on a model-name allowlist: it's a no-op for models with no
        # thinking concept, so no new local model needs to be added to a list
        # here to be safe. llama.cpp/vLLM-only param -- scoped to local, not
        # "any non-Gemini", now that real cloud gateways (OpenRouter) are
        # also routed through this same non-Gemini OpenAI-compat path and a
        # strict upstream provider behind one could reject an unknown field
        # instead of ignoring it.
        ctk: dict = {"enable_thinking": False} if self._is_local else {}

        # NuExtract-family models don't take instructions in the message --
        # they take a bare JSON schema via this template kwarg and the raw
        # text-to-extract-from as the message content (confirmed via direct
        # API test: without this, they just echo the schema back instead of
        # extracting). Harmless to omit for models that don't understand it.
        if extraction_schema is not None:
            import json as _json
            ctk["template"] = _json.dumps(extraction_schema)

        if ctk:
            payload["chat_template_kwargs"] = ctk

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        extraction_schema: dict | None = None,
    ) -> str:
        """Send a chat completion request and return the assistant message text.

        extraction_schema: for NuExtract-family extraction models only (local/
        llama.cpp). Ignored (via native Gemini path) for cloud providers.
        """
        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens, extraction_schema)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        is_cloud = base_url.startswith(("https://generativelanguage.googleapis.com", "https://api.openai.com"))
        timeout = _TIMEOUT if is_cloud else _LOCAL_TIMEOUT
        log.info("LLM provider: %s  model: %s  timeout: %ds", base_url, model, timeout)
        _instance = LLMClient(base_url, model, api_key, timeout=timeout)
    return _instance


_score_instance: LLMClient | None = None


def get_score_client() -> LLMClient:
    """LLM client for the scoring stage specifically.

    Scoring is high-volume (every discovered job) but low-complexity (a
    single 1-10 classification call) -- the opposite profile from tailoring/
    cover-letters/apply, which are low-volume (only score>=7 jobs) but
    quality-sensitive. A local model set via LLM_URL is great for the latter
    but far too slow in aggregate for the former on consumer hardware.

    Prefers a cloud provider for scoring specifically, even when LLM_URL is
    set for everything else. Resolution order:

      1. SCORE_LLM_URL/SCORE_LLM_MODEL/SCORE_LLM_API_KEY -- an explicit
         gateway (e.g. OpenRouter), same shape as TAILOR_LLM_*. Falls back to
         TAILOR_LLM_API_KEY for the key so one gateway credential covers both.
      2. GEMINI_API_KEY, then OPENAI_API_KEY.
      3. get_client() -- whatever LLM_URL points at.

    Step 1 exists because step 3 is a silent failure mode: score_job() catches
    every exception and returns score=0, and run_scoring() then writes that 0
    to the DB. A local server being down therefore looks exactly like "this
    job is a bad match" -- and because pending_score selects on
    `fit_score IS NULL`, those rows are never retried. Keeping scoring on a
    gateway that is independent of the local GPU box avoids that whole class
    of quiet data loss.
    """
    global _score_instance
    if _score_instance is None:
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        model_override = os.environ.get("SCORE_LLM_MODEL", "")

        score_url = os.environ.get("SCORE_LLM_URL", "")
        if score_url:
            base_url = score_url.rstrip("/")
            model = model_override or "local-model"
            api_key = (os.environ.get("SCORE_LLM_API_KEY", "")
                       or os.environ.get("TAILOR_LLM_API_KEY", ""))
        elif gemini_key:
            base_url, model, api_key = (_GEMINI_COMPAT_BASE, model_override or "gemini-2.0-flash", gemini_key)
        elif openai_key:
            base_url, model, api_key = ("https://api.openai.com/v1", model_override or "gpt-4o-mini", openai_key)
        else:
            return get_client()

        log.info("LLM provider (score): %s  model: %s", base_url, model)
        _score_instance = LLMClient(base_url, model, api_key, timeout=_TIMEOUT)
    return _score_instance


_enrich_instance: LLMClient | None = None
_cover_instance: LLMClient | None = None
_tailor_instance: LLMClient | None = None


def get_tailor_client() -> LLMClient:
    """LLM client for resume tailoring specifically.

    Reads TAILOR_LLM_URL/TAILOR_LLM_MODEL/TAILOR_LLM_API_KEY if set -- unlike
    enrich/cover, this one takes an API key since it's meant to point at a
    real cloud gateway (e.g. OpenRouter: https://openrouter.ai/api/v1) rather
    than only a local server. Falls back to get_client() (today's shared
    default) if unset.

    Tailoring is the pipeline's quality-critical, low-volume stage (the
    fabrication validator actively rejects most attempts), so cost matters
    far less here than getting a model that's both fast and genuinely
    faithful to the source resume -- worth paying a fraction of a cent per
    job for, rather than optimizing for zero marginal cost like score does.
    """
    global _tailor_instance
    if _tailor_instance is None:
        url = os.environ.get("TAILOR_LLM_URL", "")
        if not url:
            return get_client()
        model = os.environ.get("TAILOR_LLM_MODEL", "") or "local-model"
        api_key = os.environ.get("TAILOR_LLM_API_KEY", "")
        log.info("LLM provider (tailor): %s  model: %s", url, model)
        _tailor_instance = LLMClient(url.rstrip("/"), model, api_key, timeout=_LOCAL_TIMEOUT)
    return _tailor_instance


def resolve_apply_provider() -> tuple[str, str, str]:
    """Provider for the auto-apply browser agent: (base_url, model, api_key).

    Returns raw connection details rather than an LLMClient because the apply
    agent drives its own OpenAI-style function-calling loop over httpx (see
    apply/local_agent.py) instead of the single-shot completion path the rest
    of the pipeline uses.

    Reads APPLY_LLM_URL/APPLY_LLM_MODEL/APPLY_LLM_API_KEY. If those are unset
    it falls back to the TAILOR_LLM_* gateway (already an OpenRouter-style
    cloud endpoint), and only then to the shared default. The point of the
    override is to keep auto-apply off the Claude Code CLI engine, which bills
    Claude usage per application -- an OpenRouter Gemini model does the same
    form-filling for a fraction of a cent.

    Note the model choice matters more here than elsewhere: apply runs a
    multi-turn tool-calling loop (MAX_TURNS=40), so a model that drops or
    malforms tool calls will burn the whole budget without submitting.
    Prefer a full flash-class model over a "lite" one.
    """
    url = os.environ.get("APPLY_LLM_URL", "") or os.environ.get("TAILOR_LLM_URL", "")
    if not url:
        return _detect_provider()

    model = (os.environ.get("APPLY_LLM_MODEL", "")
             or os.environ.get("TAILOR_LLM_MODEL", "")
             or "local-model")
    api_key = (os.environ.get("APPLY_LLM_API_KEY", "")
               or os.environ.get("TAILOR_LLM_API_KEY", ""))

    log.info("LLM provider (apply): %s  model: %s", url, model)
    return url.rstrip("/"), model, api_key


def get_enrich_client() -> LLMClient:
    """LLM client for enrichment's structured-extraction fallback (only used
    when a site's structured scraping comes back empty).

    Reads ENRICH_LLM_URL/ENRICH_LLM_MODEL if set (point this at a model
    fine-tuned for text+schema -> JSON extraction, not a generic chat model --
    purpose-built extraction models substantially outperform similarly-sized
    general models at this specific task shape). Falls back to get_client()
    (today's shared default) if unset.
    """
    global _enrich_instance
    if _enrich_instance is None:
        url = os.environ.get("ENRICH_LLM_URL", "")
        if not url:
            return get_client()
        model = os.environ.get("ENRICH_LLM_MODEL", "") or "local-model"
        log.info("LLM provider (enrich): %s  model: %s", url, model)
        _enrich_instance = LLMClient(url.rstrip("/"), model, "", timeout=_LOCAL_TIMEOUT)
    return _enrich_instance


def get_cover_client() -> LLMClient:
    """LLM client for cover-letter generation specifically.

    Reads COVER_LLM_URL/COVER_LLM_MODEL if set. Falls back to get_client()
    (today's shared default -- e.g. the tailor-quality GPU model) if unset,
    so this is opt-in and doesn't change behavior until configured.
    """
    global _cover_instance
    if _cover_instance is None:
        url = os.environ.get("COVER_LLM_URL", "")
        if not url:
            return get_client()
        model = os.environ.get("COVER_LLM_MODEL", "") or "local-model"
        # Takes a key so this can point at a cloud gateway, not only a local
        # server. Falls back to the tailor credential so one OpenRouter key
        # covers score/tailor/cover/apply.
        api_key = (os.environ.get("COVER_LLM_API_KEY", "")
                   or os.environ.get("TAILOR_LLM_API_KEY", ""))
        log.info("LLM provider (cover): %s  model: %s", url, model)
        _cover_instance = LLMClient(url.rstrip("/"), model, api_key, timeout=_LOCAL_TIMEOUT)
    return _cover_instance
