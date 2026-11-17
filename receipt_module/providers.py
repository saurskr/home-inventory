"""Vision-model providers: Ollama (local) and Anthropic Claude (bring your own key).

Standard library only. A provider exposes one method:

    provider.chat_json(prompt, schema, image_b64, media_type) -> dict | str

The model is asked to answer in JSON that matches `schema`. Ollama returns the JSON as text,
Claude returns a parsed object (forced tool call); callers should pass either through
`receipts.parse_json_loose`. Problems raise `ProviderError` with a message that is safe to show
to an end user (it never contains the API key).
"""
import json
import re
import socket
import urllib.error
import urllib.request


class ProviderError(Exception):
    """kind: unreachable | timeout | model_missing | no_vision | auth | rate_limit | http"""

    def __init__(self, message, kind="http", status=None):
        super().__init__(message)
        self.message, self.kind, self.status = message, kind, status


class _HttpFailure(Exception):
    def __init__(self, status=None, timeout=False, detail=""):
        super().__init__(detail)
        self.status, self.timeout, self.detail = status, timeout, detail


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method, url, payload=None, headers=None, timeout=60, local=False):
    """JSON request helper. local=True never goes through a system proxy."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    opener = _NO_PROXY if local else urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise _HttpFailure(status=e.code, detail=e.read()[:2000].decode("utf-8", "replace"))
    except (socket.timeout, TimeoutError):
        raise _HttpFailure(timeout=True, detail="timed out")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError)):
            raise _HttpFailure(timeout=True, detail="timed out")
        raise _HttpFailure(detail=str(e))


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url="http://localhost:11434", model="qwen2.5vl:7b", timeout=300):
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout

    def chat_json(self, prompt, schema, image_b64, media_type="image/jpeg"):
        try:
            out = http_json("POST", self.base_url + "/api/chat", {
                "model": self.model, "stream": False, "format": schema, "options": {"temperature": 0},
                "messages": [{"role": "user", "content": prompt, "images": [image_b64]}]},
                timeout=self.timeout, local=True)
        except _HttpFailure as e:
            if e.timeout:
                raise ProviderError("Ollama took longer than %d minutes. Try a smaller model such as "
                                    "qwen2.5vl:3b." % (self.timeout // 60), "timeout")
            if e.status is None:
                raise ProviderError("Could not reach Ollama at %s. Is it running? Start it with "
                                    "'ollama serve'." % self.base_url, "unreachable")
            if e.status == 404:
                raise ProviderError("Model '%s' is not installed in Ollama. Run: ollama pull %s"
                                    % (self.model, self.model), "model_missing", 404)
            if e.status == 400 and re.search(r"image|vision|multimodal", e.detail or "", re.I):
                raise ProviderError("%s can't read images. Choose a vision model (for example qwen2.5vl)." % self.model,
                                    "no_vision", 400)
            raise ProviderError("Ollama returned an error (HTTP %s)." % e.status, "http", e.status)
        return (out.get("message") or {}).get("content", "")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key, model="claude-haiku-5-5", base_url="https://api.anthropic.com", timeout=90):
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.base_url = base_url.rstrip("/")

    def chat_json(self, prompt, schema, image_b64, media_type="image/jpeg"):
        tool = {"name": "record", "description": "Record the answer.", "input_schema": schema}
        try:
            out = http_json("POST", self.base_url + "/v1/messages", {
                "model": self.model, "max_tokens": 2000, "tools": [tool],
                "tool_choice": {"type": "tool", "name": "record"},
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": prompt}]}]},
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                timeout=self.timeout)
        except _HttpFailure as e:
            if e.timeout or e.status is None:
                raise ProviderError("Could not reach the Anthropic API. Check your internet connection.", "unreachable")
            if e.status in (401, 403):
                raise ProviderError("Anthropic rejected the API key.", "auth", e.status)
            if e.status == 404:
                raise ProviderError("Anthropic does not know the model '%s'." % self.model, "model_missing", 404)
            if e.status == 429:
                raise ProviderError("Anthropic rate limit or spending limit reached. Try again later.",
                                    "rate_limit", 429)
            raise ProviderError("Anthropic returned an error (HTTP %s)." % e.status, "http", e.status)
        block = next((c for c in out.get("content", []) if c.get("type") == "tool_use"), None)
        return block.get("input") if block else None
