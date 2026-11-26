"""Talk to an agent from your browser, locally and for free.

  uv run python scripts/browser_test_call.py [--template restaurant_receptionist]
                                             [--business "Joe's Diner"] [--timezone Asia/Kolkata]

Needs the API on :8000, the agent worker, Ollama, and `docker compose --profile speech up -d`.
Creates a demo account, an agent from the template (open every day 8:00-20:00), saves a
version, starts a test call, and opens a small page in your browser that joins the call
with your microphone. When you hang up, the transcript is printed here.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
import time
import webbrowser
from pathlib import Path

import httpx

API = "http://127.0.0.1:8000"
PORT = 5180
OUT = Path(__file__).resolve().parent.parent / ".e2e"
# Pinned browser SDK (published 2026-10-16).
SDK = "https://cdn.jsdelivr.net/npm/livekit-client@2.23.0/dist/livekit-client.umd.js"

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Test call</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#1b1f24;background:#fafafa}
 button{font-size:17px;padding:12px 22px;border-radius:10px;border:0;cursor:pointer;margin-right:8px}
 #start{background:#1f7a4d;color:#fff} #stop{background:#b42318;color:#fff}
 #log{margin-top:20px;font-size:14px;white-space:pre-wrap;background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px;min-height:120px}
</style></head><body>
<h2>Test call: __NAME__</h2>
<p>Press <b>Start call</b>, allow the microphone, and talk to the agent like a phone call.</p>
<button id="start">Start call</button><button id="stop" disabled>Hang up</button>
<div id="log">Ready.</div>
<script src="__SDK__"></script>
<script>
const URL_ = __URL__, TOKEN = __TOKEN__;
const log = (m) => { document.getElementById('log').textContent += "\\n" + m; };
let room;
document.getElementById('start').onclick = async () => {
  document.getElementById('start').disabled = true;
  room = new LivekitClient.Room();
  room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === 'audio') { document.body.appendChild(track.attach()); log('Agent connected. Say hello.'); }
  });
  room.on(LivekitClient.RoomEvent.TranscriptionReceived, (segs, who) => {
    for (const s of segs) if (s.final) log((who && who.isLocal ? 'You: ' : 'Agent: ') + s.text);
  });
  room.on(LivekitClient.RoomEvent.Disconnected, () => log('Call ended.'));
  await room.connect(URL_, TOKEN);
  await room.localParticipant.setMicrophoneEnabled(true);
  document.getElementById('stop').disabled = false;
  log('Connected. Waiting for the agent...');
};
document.getElementById('stop').onclick = async () => { if (room) await room.disconnect(); };
</script></body></html>"""


def demo_login(c: httpx.Client, business: str) -> dict[str, str]:
    """Sign in to the local demo account, creating it the first time."""
    creds = {"email": "demo-owner@example.com", "password": "demo-password-123"}
    r = c.post("/api/v1/auth/login", json=creds)
    if r.status_code == 401:
        r = c.post(
            "/api/v1/auth/signup",
            json={**creds, "full_name": "Demo Owner", "organization_name": business},
        )
    r.raise_for_status()
    return {"x-csrf-token": r.json()["csrf_token"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="restaurant_receptionist")
    ap.add_argument("--business", default="Joe's Diner")
    ap.add_argument("--timezone", default="Asia/Kolkata")
    ap.add_argument("--no-open", action="store_true", help="print the link instead of opening it")
    args = ap.parse_args()

    c = httpx.Client(base_url=API, timeout=30)
    h = demo_login(c, args.business)
    c.patch("/api/v1/organizations/current", json={"timezone": args.timezone}, headers=h)
    agent = c.post(
        "/api/v1/agents", json={"name": "Front desk", "template_key": args.template}, headers=h
    ).json()
    draft = c.get(f"/api/v1/agents/{agent['id']}/configuration").json()
    cfg = draft["config"]
    cfg["business"]["hours"] = {
        d: [{"open": "08:00", "close": "20:00"}]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }
    c.put(
        f"/api/v1/agents/{agent['id']}/configuration",
        json={"revision": draft["revision"], "config": cfg},
        headers=h,
    ).raise_for_status()
    c.post(f"/api/v1/agents/{agent['id']}/versions", json={}, headers=h).raise_for_status()
    call = c.post(f"/api/v1/agents/{agent['id']}/test-calls", json={}, headers=h)
    call.raise_for_status()
    info = call.json()

    OUT.mkdir(exist_ok=True)
    page = (
        PAGE.replace("__SDK__", SDK)
        .replace("__NAME__", args.business)
        .replace("__URL__", json.dumps(info["server_url"]))
        .replace("__TOKEN__", json.dumps(info["token"]))
    )
    (OUT / "call.html").write_text(page, encoding="utf-8")

    handler = lambda *a: http.server.SimpleHTTPRequestHandler(*a, directory=str(OUT))  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/call.html"
    print(f"Opening {url}  (call {info['call_id']}, token valid 10 minutes)")
    if not args.no_open:
        webbrowser.open(url)

    print("Waiting for the call to finish... (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3)
            d = c.get(f"/api/v1/calls/{info['call_id']}").json()
            if d["status"] in ("completed", "transferred", "failed"):
                break
    except KeyboardInterrupt:
        d = c.get(f"/api/v1/calls/{info['call_id']}").json()
    print(f"\nCall {d['status']} ({d['end_reason']}), {d['turns']} turns")
    for t in d.get("transcript") or []:
        print(f"  {t['role']:>6}: {t['text']}")
    httpd.shutdown()


if __name__ == "__main__":
    main()
