from fastapi import APIRouter

from app.providers.ollama import OllamaProvider

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "personal-ai-assistant"}


@router.get("/ollama/health")
async def ollama_health():
    provider = OllamaProvider()
    return await provider.health()


@router.get("/models")
async def list_models():
    provider = OllamaProvider()
    return await provider.list_models()
