"""
One chat() function that talks to four different backends:
  lmstudio / openai  -> the OpenAI /chat/completions endpoint
  ollama             -> its native /api/chat (so we can set the context size)
  claude             -> the Anthropic SDK (pip install anthropic)

Messages always use the OpenAI shape:
    [{"role": "system"|"user"|"assistant", "content": "..."}]
"""

import json
import re

import requests

import config

# removing reasoning blocks from being included in the reply
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


# why the last reply came back empty (finish reason, hidden reasoning, etc.)
last_empty_reason = ""


def describe():
    where = config.LLM_BASE_URL or "api.anthropic.com"
    return "%s / %s @ %s" % (config.LLM_PROVIDER, config.LLM_MODEL, where)


def check_config():
    provider = config.LLM_PROVIDER
    if provider not in ("lmstudio", "ollama", "openai", "claude"):
        raise LLMError("LLM_PROVIDER must be lmstudio, ollama, openai or claude (got %r)" % provider)
    if not config.LLM_MODEL:
        raise LLMError("LLM_MODEL is not set in .env")
    if provider in ("openai", "claude") and not config.LLM_API_KEY:
        raise LLMError("LLM_API_KEY is required for provider %r" % provider)


def chat(messages, temperature=None):
    """Send a conversation, return the assistant's text (reasoning tags stripped)."""
    if temperature is None:
        temperature = config.LLM_TEMPERATURE
    elif config.LLM_TEMPERATURE is None:
        # hosted provider and no temperature configured - leave the API default
        temperature = None

    global last_empty_reason
    last_empty_reason = ""

    provider = config.LLM_PROVIDER
    if provider == "claude":
        text, why = chat_claude(messages)
    elif provider == "ollama":
        text, why = chat_ollama(messages, temperature)
    else:
        text, why = chat_openai(messages, temperature)

    cleaned = THINK_RE.sub("", text or "").strip()
    if not cleaned:
        if text and text.strip():
            why += "; reply was only a <think> block with no answer after it"
        last_empty_reason = why
    return cleaned


def chat_openai(messages, temperature):
    payload = {"model": config.LLM_MODEL, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature

    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = "Bearer " + config.LLM_API_KEY

    url = config.LLM_BASE_URL + "/chat/completions"
    resp = requests.post(url, headers=headers, json=payload, timeout=config.LLM_TIMEOUT)
    if resp.status_code >= 400:
        raise LLMError("%s HTTP %s: %s" % (config.LLM_PROVIDER, resp.status_code, resp.text[:500]))

    data = resp.json()
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as e:
        raise LLMError("Unexpected response shape: %s" % data) from e

    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    usage = data.get("usage") or {}
    why = "finish_reason=%s, completion_tokens=%s, hidden reasoning chars=%d" % (
        choice.get("finish_reason"), usage.get("completion_tokens"), len(reasoning))
    return msg.get("content") or "", why


def chat_ollama(messages, temperature):
    options = {"num_ctx": config.OLLAMA_NUM_CTX}
    if temperature is not None:
        options["temperature"] = temperature

    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    if config.OLLAMA_THINK:
        # "false" turns thinking off (qwen3, deepseek-r1...);
        # gpt-oss can't turn it off, only "low" / "medium" / "high"
        t = config.OLLAMA_THINK.lower()
        payload["think"] = {"true": True, "false": False}.get(t, t)
    url = config.LLM_BASE_URL + "/api/chat"
    resp = requests.post(url, json=payload, timeout=config.LLM_TIMEOUT)
    if resp.status_code >= 400:
        raise LLMError("ollama HTTP %s: %s" % (resp.status_code, resp.text[:500]))
    data = resp.json()
    msg = data.get("message", {})
    content = msg.get("content") or ""
    thinking = msg.get("thinking") or ""
    tool_calls = msg.get("tool_calls") or []

    why = "done_reason=%s, eval_count=%s, prompt_eval_count=%s, thinking chars=%d, tool_calls=%d" % (
        data.get("done_reason"), data.get("eval_count"), data.get("prompt_eval_count"),
        len(thinking), len(tool_calls))

    if content.strip():
        return content, why

    # Some thinking models (gpt-oss, qwen3, ...) emit a *native* tool call
    # instead of the JSON text we asked for. Ollama moves that into
    # message.tool_calls and leaves content empty. Turn it back into our format.
    if tool_calls:
        fn = tool_calls[0].get("function", {})
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"query": args}
        action = {"action": fn.get("name", "")}
        action.update(args)
        return json.dumps(action), why + " (recovered native tool call)"

    # Otherwise the whole reply got classified as "thinking" (no answer after
    # it). If the JSON or the notes ended up in there, use it rather than
    # throwing it away.
    if thinking.strip():
        return thinking, why + " (used thinking as reply)"

    return "", why


# keep the Anthropic client around so we don't rebuild it every call
claude_client = None


def chat_claude(messages):
    global claude_client

    try:
        import anthropic
    except ImportError as e:
        raise LLMError("Provider 'claude' needs the SDK: pip install anthropic") from e

    if claude_client is None:
        kwargs = {"api_key": config.LLM_API_KEY, "timeout": config.LLM_TIMEOUT}
        if config.LLM_BASE_URL:
            kwargs["base_url"] = config.LLM_BASE_URL
        claude_client = anthropic.Anthropic(**kwargs)

    # Claude wants the system prompt passed separately, not as a message
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo = [{"role": m["role"], "content": m["content"]}
             for m in messages if m["role"] != "system"]

    kwargs = {
        "model": config.LLM_MODEL,
        "max_tokens": config.LLM_MAX_TOKENS,
        "messages": convo,
    }
    if system:
        kwargs["system"] = system
    if config.LLM_EFFORT:
        kwargs["output_config"] = {"effort": config.LLM_EFFORT}
    if config.CLAUDE_FALLBACKS.lower() != "off":
        # if the model declines the request, let the API retry on a fallback
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = config.CLAUDE_FALLBACKS

    try:
        response = claude_client.beta.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        raise LLMError("claude HTTP %s: %s" % (e.status_code, e.message)) from e
    except anthropic.APIConnectionError as e:
        raise LLMError("claude connection error: %s" % e) from e

    if response.stop_reason == "refusal":
        raise LLMError("Claude declined this request (stop_reason=refusal)")

    why = "stop_reason=%s, output_tokens=%s, blocks=%s" % (
        response.stop_reason, getattr(response.usage, "output_tokens", "?"),
        [b.type for b in response.content])
    return "".join(b.text for b in response.content if b.type == "text"), why
