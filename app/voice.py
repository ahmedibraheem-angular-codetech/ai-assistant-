"""Voice on this Mac: speech to text with whisper.cpp, text to speech with `say`.

Nothing leaves the computer. Every external program runs with an argument
list (no shell), inside a private temporary folder that is deleted after the
call, with a time limit. Audio and text sizes are capped before any work.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

AFCONVERT = "/usr/bin/afconvert"
SAY = "/usr/bin/say"
# Containers WebKit's MediaRecorder or a file picker may give us; afconvert reads all of them.
AUDIO_TYPES = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "audio/mpeg": ".mp3",
    "audio/caf": ".caf",
}
_VOICE_NAME = re.compile(r"^[A-Za-z][A-Za-z ()\-]{0,63}$")


class VoiceError(RuntimeError):
    """A short, user-facing reason the voice call failed."""


class VoiceUnavailable(VoiceError):
    """The local engine is not set up."""


@dataclass(frozen=True)
class Speech:
    audio: bytes
    mime: str = "audio/mp4"


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as exc:
        raise VoiceError("The voice engine took too long.") from exc
    except OSError as exc:
        raise VoiceUnavailable(f"Cannot run {Path(args[0]).name}.") from exc


def _base_type(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def stt_ready() -> bool:
    settings = get_settings()
    model = Path(settings.voice_whisper_model) if settings.voice_whisper_model else None
    return bool(model and model.is_file() and Path(settings.voice_whisper_cli).is_file())


@lru_cache(maxsize=1)
def installed_voices() -> tuple[str, ...]:
    """Names of the Arabic and English voices `say` has on this Mac."""
    try:
        out = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ()
    voices = []
    for line in out.splitlines():
        match = re.match(r"^(.+?)\s{2,}([a-z]{2})_[A-Za-z0-9]+\s", line)
        if match and match.group(2) in {"ar", "en"} and _VOICE_NAME.match(match.group(1).strip()):
            voices.append(match.group(1).strip())
    return tuple(voices)


def tts_ready() -> bool:
    return Path(SAY).is_file() and Path(AFCONVERT).is_file()


def status() -> dict:
    settings = get_settings()
    return {
        "stt_ready": stt_ready(),
        "tts_ready": tts_ready(),
        "language": settings.voice_language,
        "model": Path(settings.voice_whisper_model).name if settings.voice_whisper_model else None,
        "voice": settings.voice_tts_voice,
        "voices": list(installed_voices()),
        "max_audio_bytes": settings.voice_max_audio_bytes,
        "max_tts_chars": settings.voice_max_tts_chars,
    }


def transcribe(audio: bytes, mime: str) -> str:
    """Audio from the microphone -> text. Blocking: call it from a thread."""
    settings = get_settings()
    if not stt_ready():
        raise VoiceUnavailable("Speech input is not set up. Set VOICE_WHISPER_MODEL to a ggml model file.")
    suffix = AUDIO_TYPES.get(_base_type(mime))
    if suffix is None:
        raise VoiceError("That audio format is not supported.")
    if not audio:
        raise VoiceError("The recording is empty.")
    if len(audio) > settings.voice_max_audio_bytes:
        raise VoiceError("The recording is too long.")
    with tempfile.TemporaryDirectory(prefix="assistant-voice-") as tmp:
        work = Path(tmp)
        source = work / f"input{suffix}"
        source.write_bytes(audio)
        wav = work / "speech.wav"
        # whisper.cpp wants 16 kHz mono 16-bit PCM.
        converted = _run(
            [AFCONVERT, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(source), str(wav)],
            work,
            settings.voice_timeout_seconds,
        )
        if converted.returncode != 0 or not wav.is_file():
            raise VoiceError("The recording could not be read.")
        result = _run(
            [
                settings.voice_whisper_cli,
                "-m", settings.voice_whisper_model,
                "-l", settings.voice_language,
                "-f", str(wav),
                "-nt", "-np", "-otxt", "-of", str(work / "speech"),
            ],
            work,
            settings.voice_timeout_seconds,
        )
        text_file = work / "speech.txt"
        if result.returncode != 0 or not text_file.is_file():
            raise VoiceError("Speech recognition failed.")
        return " ".join(text_file.read_text(encoding="utf-8", errors="replace").split())


def speak(text: str, voice: str | None = None) -> Speech:
    """Text -> spoken audio (AAC in an .m4a container). Blocking: call it from a thread."""
    settings = get_settings()
    if not tts_ready():
        raise VoiceUnavailable("Text to speech is not available on this computer.")
    text = text.strip()
    if not text:
        raise VoiceError("There is nothing to read.")
    if len(text) > settings.voice_max_tts_chars:
        raise VoiceError("The text is too long to read aloud.")
    chosen = (voice or settings.voice_tts_voice).strip()
    voices = installed_voices()
    if voices and chosen not in voices:
        raise VoiceError("That voice is not installed.")
    with tempfile.TemporaryDirectory(prefix="assistant-voice-") as tmp:
        work = Path(tmp)
        script = work / "text.txt"  # read from a file: text starting with "-" is never an option
        script.write_text(text, encoding="utf-8")
        aiff = work / "speech.aiff"
        said = _run([SAY, "-v", chosen, "-f", str(script), "-o", str(aiff)], work, settings.voice_timeout_seconds)
        if said.returncode != 0 or not aiff.is_file():
            raise VoiceError("Text to speech failed.")
        m4a = work / "speech.m4a"
        packed = _run([AFCONVERT, "-f", "m4af", "-d", "aac", str(aiff), str(m4a)], work, settings.voice_timeout_seconds)
        if packed.returncode != 0 or not m4a.is_file():
            raise VoiceError("Text to speech failed.")
        return Speech(audio=m4a.read_bytes())
