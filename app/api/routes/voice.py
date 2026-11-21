"""Voice for the Mac app: speech to text and text to speech, on this computer.

Admin routes (local user only). Audio travels as base64 inside JSON because
the app's token bridge carries JSON only.
"""

import asyncio
import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app import voice
from app.api.routes.permissions import require_local_admin

router = APIRouter()
# One voice job at a time: whisper uses every CPU core.
_busy = asyncio.Semaphore(1)


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_base64: str = Field(min_length=1, max_length=70_000_000)
    mime: str = Field(min_length=1, max_length=80)


class SpeakRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=20_000)
    voice: str | None = Field(default=None, max_length=64)


def _fail(exc: voice.VoiceError) -> HTTPException:
    return HTTPException(status_code=503 if isinstance(exc, voice.VoiceUnavailable) else 422, detail=str(exc))


@router.get("/voice/status")
def read_status(_local: None = Depends(require_local_admin)):
    return voice.status()


@router.post("/voice/transcribe")
async def transcribe(payload: TranscribeRequest, _local: None = Depends(require_local_admin)):
    try:
        audio = base64.b64decode(payload.audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="The audio is not valid base64.") from exc
    async with _busy:
        try:
            text = await asyncio.to_thread(voice.transcribe, audio, payload.mime)
        except voice.VoiceError as exc:
            raise _fail(exc) from exc
    return {"text": text}


@router.post("/voice/speak")
async def speak(payload: SpeakRequest, _local: None = Depends(require_local_admin)):
    async with _busy:
        try:
            speech = await asyncio.to_thread(voice.speak, payload.text, payload.voice)
        except voice.VoiceError as exc:
            raise _fail(exc) from exc
    return {"audio_base64": base64.b64encode(speech.audio).decode("ascii"), "mime": speech.mime}
