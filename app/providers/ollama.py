import httpx

from app.core.config import get_settings


class OllamaError(RuntimeError):
    pass


class OllamaProvider:
    def __init__(self):
        settings = get_settings()
        self.base_url = settings.ollama_base_url.rstrip("/")
        self.timeout = settings.ollama_timeout_seconds

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            return {
                "status": "connected",
                "ollama_url": self.base_url,
                "installed_models": len(payload.get("models", [])),
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "status": "disconnected",
                "ollama_url": self.base_url,
                "error": str(exc),
            }

    async def list_models(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
            models = [
                {
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "modified_at": item.get("modified_at"),
                }
                for item in payload.get("models", [])
            ]
            return {"models": models, "count": len(models)}
        except httpx.HTTPError as exc:
            raise OllamaError(f"Could not retrieve Ollama models: {exc}") from exc

    async def chat(
        self,
        model: str,
        message: str,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
    ) -> str:
        """Send one local Ollama chat request.

        When history is provided, it is the ordered transcript and already
        includes the current user message. That message is not appended again.
        """
        from app.core.controls import PAUSED_MESSAGE, AssistantBlockedError, is_paused

        if is_paused():
            raise AssistantBlockedError(PAUSED_MESSAGE)
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            for item in history:
                role = str(item.get("role", ""))
                content = item.get("content")
                if role not in {"user", "assistant"} or not isinstance(content, str):
                    continue
                messages.append({"role": role, "content": content})
        else:
            messages.append({"role": "user", "content": message})

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={"model": model, "messages": messages, "stream": False},
                )
                response.raise_for_status()
                payload = response.json()
            return payload["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise OllamaError(f"Ollama chat request failed: {exc}") from exc
