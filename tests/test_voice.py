"""Voice: speech to text (whisper.cpp, faked here) and text to speech (macOS say)."""

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest

from app import voice
from app.core.config import get_settings

pytestmark = pytest.mark.skipif(not Path(voice.SAY).is_file(), reason="macOS only")

FAKE_WHISPER = """#!/bin/sh
# Fake whisper-cli: record the arguments, write the transcript where -of points.
out=""
while [ $# -gt 0 ]; do
  case "$1" in -of) out="$2"; shift;; esac
  echo "$1" >> "$ARGS_LOG"
  shift
done
printf '  مرحبا   يا مساعد \\n' > "$out.txt"
"""


@pytest.fixture
def fake_whisper(tmp_path, monkeypatch):
    cli = tmp_path / "whisper-cli"
    cli.write_text(FAKE_WHISPER)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    model = tmp_path / "ggml-test.bin"
    model.write_bytes(b"x")
    log = tmp_path / "args.log"
    monkeypatch.setenv("ARGS_LOG", str(log))
    monkeypatch.setenv("VOICE_WHISPER_CLI", str(cli))
    monkeypatch.setenv("VOICE_WHISPER_MODEL", str(model))
    get_settings.cache_clear()
    yield log
    get_settings.cache_clear()


def _spoken_m4a() -> bytes:
    return voice.speak("مرحبا").audio


def test_speak_returns_aac_audio():
    speech = voice.speak("مرحبا، أنا المساعد")
    assert speech.mime == "audio/mp4"
    assert len(speech.audio) > 1_000
    assert speech.audio[4:8] == b"ftyp"  # an MP4/M4A container


def test_speak_rejects_empty_long_and_unknown_voice():
    with pytest.raises(voice.VoiceError):
        voice.speak("   ")
    with pytest.raises(voice.VoiceError):
        voice.speak("a" * (get_settings().voice_max_tts_chars + 1))
    if voice.installed_voices():
        with pytest.raises(voice.VoiceError):
            voice.speak("hello", voice="NoSuchVoice")


def test_text_starting_with_a_dash_is_read_not_parsed_as_an_option():
    assert len(voice.speak("-o /tmp/evil.aiff").audio) > 1_000
    assert not Path("/tmp/evil.aiff").exists()


def test_transcribe_converts_and_reads_the_transcript(fake_whisper):
    text = voice.transcribe(_spoken_m4a(), "audio/mp4;codecs=mp4a.40.2")
    assert text == "مرحبا يا مساعد"
    args = fake_whisper.read_text().split()
    assert "-l" in args and args[args.index("-l") + 1] == "ar"
    assert args[args.index("-f") + 1].endswith("speech.wav")


def test_transcribe_refuses_bad_input(fake_whisper):
    with pytest.raises(voice.VoiceError, match="format"):
        voice.transcribe(b"abc", "video/webm")
    with pytest.raises(voice.VoiceError, match="empty"):
        voice.transcribe(b"", "audio/mp4")
    with pytest.raises(voice.VoiceError, match="read"):
        voice.transcribe(b"not audio at all" * 100, "audio/mp4")


def test_transcribe_without_a_model_says_so(monkeypatch):
    monkeypatch.setenv("VOICE_WHISPER_MODEL", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(voice.VoiceUnavailable):
            voice.transcribe(b"x", "audio/mp4")
    finally:
        get_settings.cache_clear()


def test_voice_routes(client, fake_whisper):
    status = client.get("/api/v1/voice/status").json()
    assert status["stt_ready"] and status["tts_ready"]

    spoken = client.post("/api/v1/voice/speak", json={"text": "مرحبا"})
    assert spoken.status_code == 200
    audio = spoken.json()["audio_base64"]

    heard = client.post("/api/v1/voice/transcribe", json={"audio_base64": audio, "mime": "audio/mp4"})
    assert heard.status_code == 200 and heard.json() == {"text": "مرحبا يا مساعد"}

    bad = client.post("/api/v1/voice/transcribe", json={"audio_base64": "@@@", "mime": "audio/mp4"})
    assert bad.status_code == 422
    assert client.post("/api/v1/voice/speak", json={"text": "hi", "extra": 1}).status_code == 422


def test_voice_routes_need_the_local_admin(database_file):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/voice/status").status_code in {401, 403, 421}
