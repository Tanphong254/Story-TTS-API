import asyncio
import os
from pathlib import Path
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.setenv("TTS_API_KEY", "test-secret-token")
    monkeypatch.setenv("AUDIO_URL_SECRET", "test-link-secret")
    yield TestClient(main.app)


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_voice_list_requires_key(client):
    denied = client.get("/v1/voices")
    assert denied.status_code == 401
    allowed = client.get("/v1/voices", headers={"Authorization": "Bearer test-secret-token"})
    assert allowed.status_code == 200
    data = allowed.json()
    assert {voice["id"] for voice in data["voices"]} == {
        "vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"
    }
    assert data["rates"] == [0.75, 1.0, 1.25, 1.5, 2.0]


@pytest.mark.parametrize("rate", [0.75, 1.0, 1.25, 1.5, 2.0])
def test_rate_validation(rate):
    assert main.valid_rate(rate)


def test_invalid_rate_and_voice_are_rejected(client):
    headers = {"Authorization": "Bearer test-secret-token"}
    bad_rate = client.post("/v1/tts", headers=headers, json={
        "text": "Xin chào", "voice": "vi-VN-NamMinhNeural", "rate": 1.1
    })
    bad_voice = client.post("/v1/tts", headers=headers, json={
        "text": "Xin chào", "voice": "invalid", "rate": 1.0
    })
    assert bad_rate.status_code == 422
    assert bad_voice.status_code == 422


def test_cache_key_changes_with_content_voice_or_rate():
    base = main.build_cache_key("Nội dung tiếng Việt", "vi-VN-NamMinhNeural", 1.0)
    assert main.build_cache_key("Nội dung khác", "vi-VN-NamMinhNeural", 1.0) != base
    assert main.build_cache_key("Nội dung tiếng Việt", "vi-VN-HoaiMyNeural", 1.0) != base
    assert main.build_cache_key("Nội dung tiếng Việt", "vi-VN-NamMinhNeural", 1.25) != base


def test_job_is_queued_and_audio_cached_for_repeat_request(client, monkeypatch):
    synthesis_calls = []

    async def fake_synthesize(text, voice, rate, output):
        synthesis_calls.append((text, voice, rate))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-mp3-data-" * 50)

    monkeypatch.setattr(main, "synthesize_to_file", fake_synthesize)
    headers = {"Authorization": "Bearer test-secret-token"}
    payload = {
        "chapter_id": "book-1/chapter-1",
        "text": "Nội dung chương cần đọc.",
        "voice": "vi-VN-NamMinhNeural",
        "rate": 1.25,
    }

    first = client.post("/v1/tts", headers=headers, json=payload)
    assert first.status_code == 202
    first_id = first.json()["job_id"]

    deadline = monotonic() + 3
    result = None
    while monotonic() < deadline:
        result = client.get(f"/v1/jobs/{first_id}", headers=headers)
        if result.status_code == 200 and result.json()["status"] in {"completed", "failed"}:
            break
        sleep(0.02)

    assert result is not None and result.status_code == 200
    assert result.json()["status"] == "completed"
    assert result.json()["audio_url"]
    assert len(synthesis_calls) == 1

    again = client.post("/v1/tts", headers=headers, json=payload)
    assert again.status_code == 200
    assert again.json()["status"] == "completed"
    assert again.json()["cache_hit"] is True
    assert again.json()["audio_url"]
    assert len(synthesis_calls) == 1

    audio = client.get(again.json()["audio_url"])
    assert audio.status_code == 200
    assert audio.content.startswith(b"fake-mp3-data-")
    downloaded = client.get(again.json()["download_url"])
    assert downloaded.status_code == 200
    assert "attachment" in downloaded.headers["content-disposition"]


def test_missing_api_key_fails_closed(client, monkeypatch):
    monkeypatch.delenv("TTS_API_KEY")
    response = client.get("/v1/voices")
    assert response.status_code == 503


def test_same_pending_request_is_reused(client, monkeypatch):
    async def wait_then_write(text, voice, rate, output):
        await asyncio.sleep(0.2)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * 200)

    monkeypatch.setattr(main, "synthesize_to_file", wait_then_write)
    headers = {"Authorization": "Bearer test-secret-token"}
    payload = {"text": "Nội dung duy nhất", "voice": "vi-VN-HoaiMyNeural", "rate": 1.0}
    first = client.post("/v1/tts", headers=headers, json=payload)
    second = client.post("/v1/tts", headers=headers, json=payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["reused_request"] is True


def test_invalid_signed_audio_url_rejected(client):
    response = client.get("/v1/jobs/5/audio?expires=9999999999&token=" + ("a" * 64))
    assert response.status_code == 403
