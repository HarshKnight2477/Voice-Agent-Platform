"""End-to-end voice test: a simulated caller talks to the agent over a real LiveKit call.

Needs: docker compose --profile speech up -d, the API (uvicorn) on :8000, the agent worker
(python -m voice_platform.voice.livekit_worker dev) and Ollama running.

  uv run python scripts/e2e_voice_call.py

Steps: sign up -> create an agent -> save a version -> start a browser test call -> join
the room as the caller -> record the agent's greeting -> speak a question (synthesised with
a different voice) -> record the reply -> transcribe both with Whisper -> check the call
record in the API. Prints latency and transcripts.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from livekit import rtc

API = "http://127.0.0.1:8000"
TTS = "http://127.0.0.1:8880/v1/audio/speech"
STT = "http://127.0.0.1:8001/v1/audio/transcriptions"
STT_MODEL = "Systran/faster-whisper-base.en"
OUT = Path(__file__).resolve().parent.parent / ".e2e"
SAMPLE_RATE = 24000
FRAME_MS = 20


def synthesize(text: str, voice: str = "am_michael") -> np.ndarray:
    r = httpx.post(
        TTS,
        json={"model": "kokoro", "voice": voice, "input": text, "response_format": "wav"},
        timeout=60,
    )
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getframerate() == SAMPLE_RATE, w.getframerate()
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def to_wav(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def transcribe(wav: bytes) -> str:
    r = httpx.post(
        STT,
        files={"file": ("a.wav", wav, "audio/wav")},
        data={"model": STT_MODEL, "language": "en"},
        timeout=120,
    )
    r.raise_for_status()
    return str(r.json()["text"]).strip()


def setup_call() -> dict[str, str]:
    c = httpx.Client(base_url=API, timeout=30)
    creds = {"email": "demo-owner@example.com", "password": "demo-password-123"}
    s = c.post("/api/v1/auth/login", json=creds)
    if s.status_code == 401:  # first run: create the local demo account
        s = c.post(
            "/api/v1/auth/signup",
            json={**creds, "full_name": "Demo Owner", "organization_name": "Sunrise Cafe"},
        )
    s.raise_for_status()
    h = {"x-csrf-token": s.json()["csrf_token"]}
    agent = c.post("/api/v1/agents", json={"name": "Front desk"}, headers=h).json()
    draft = c.get(f"/api/v1/agents/{agent['id']}/configuration").json()
    cfg = draft["config"]
    cfg["business"]["hours"] = {
        d: [{"open": "08:00", "close": "20:00"}]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    cfg["greeting"] = "Hi, thanks for calling Sunrise Cafe. How can I help?"
    r = c.put(
        f"/api/v1/agents/{agent['id']}/configuration",
        json={"revision": draft["revision"], "config": cfg},
        headers=h,
    )
    r.raise_for_status()
    c.post(f"/api/v1/agents/{agent['id']}/versions", json={}, headers=h).raise_for_status()
    call = c.post(f"/api/v1/agents/{agent['id']}/test-calls", json={}, headers=h)
    call.raise_for_status()
    out = call.json()
    out["cookie"] = c.cookies.get("vp_session") or ""
    return out


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    question = synthesize("Hi, what time do you close today?")
    print(f"caller question audio: {len(question) / SAMPLE_RATE:.1f}s")
    call = setup_call()
    print(f"test call {call['call_id']} room {call['room']}")

    room = rtc.Room()
    received: list[np.ndarray] = []
    first_audio_at: list[float] = []
    agent_joined = asyncio.Event()

    async def record(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for ev in stream:
            data = np.frombuffer(ev.frame.data, dtype=np.int16)
            if np.abs(data).max(initial=0) > 300 and not first_audio_at:
                first_audio_at.append(time.perf_counter())
            received.append(data.copy())

    @room.on("track_subscribed")
    def _on_track(
        track: rtc.Track, pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant
    ) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            agent_joined.set()
            asyncio.ensure_future(record(track))

    t0 = time.perf_counter()
    await room.connect(call["server_url"], call["token"])
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("caller-mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    spf = SAMPLE_RATE * FRAME_MS // 1000

    async def send(samples: np.ndarray) -> None:
        for i in range(0, len(samples), spf):
            chunk = samples[i : i + spf]
            if len(chunk) < spf:
                chunk = np.pad(chunk, (0, spf - len(chunk)))
            await source.capture_frame(rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, spf))

    async def silence(seconds: float) -> None:
        await send(np.zeros(int(SAMPLE_RATE * seconds), dtype=np.int16))

    await asyncio.wait_for(agent_joined.wait(), timeout=60)
    print(f"agent audio track subscribed after {time.perf_counter() - t0:.1f}s")
    await silence(9)  # let the greeting play
    greeting = np.concatenate(received) if received else np.zeros(1, np.int16)
    received.clear()
    first_audio_at.clear()

    await send(question)
    spoke_at = time.perf_counter()
    await silence(14)  # caller listens
    reply = np.concatenate(received) if received else np.zeros(1, np.int16)
    await room.disconnect()

    (OUT / "greeting.wav").write_bytes(to_wav(greeting, SAMPLE_RATE))
    (OUT / "reply.wav").write_bytes(to_wav(reply, SAMPLE_RATE))
    g_text = transcribe(to_wav(greeting, SAMPLE_RATE))
    r_text = transcribe(to_wav(reply, SAMPLE_RATE))
    latency = (first_audio_at[0] - spoke_at) if first_audio_at else None
    print(f"agent greeting heard: {g_text!r}")
    print(f"agent reply heard:    {r_text!r}")
    print(
        "time from end of caller speech to first agent audio: "
        + (f"{latency:.2f}s" if latency is not None else "no reply audio")
    )

    await asyncio.sleep(3)
    c = httpx.Client(base_url=API, cookies={"vp_session": call["cookie"]}, timeout=30)
    detail = c.get(f"/api/v1/calls/{call['call_id']}").json()
    print(
        f"call record: status={detail['status']} turns={detail['turns']} "
        f"end_reason={detail['end_reason']}"
    )
    print(
        "stored transcript:",
        json.dumps([(t["role"], t["text"]) for t in detail["transcript"]], ensure_ascii=False),
    )
    ok = "sunrise" in g_text.lower() and r_text and detail["turns"] >= 1
    print("RESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    asyncio.run(main())
