#!/usr/bin/env python3
"""Mailbox Watch — find people using mailboxes in the VSS archive."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

VSS_URL = os.environ.get("VSS_URL", os.environ.get("INGRESS_URL", "")).rstrip("/")
VSS_USERNAME = os.environ.get("VSS_USERNAME", os.environ.get("USERNAME", ""))
VSS_PASSWORD = os.environ.get("VSS_PASSWORD", os.environ.get("PASSWORD", ""))
PORT = int(os.environ.get("PORT", "8080"))

DEFAULT_QUERY = "person using a mailbox depositing mail on the sidewalk"

MAILBOX_RE = re.compile(r"\b(mail\s*box(?:es)?|posting mail|deposit(?:ing|s)? (?:the )?mail)\b", re.I)
PERSON_RE = re.compile(
    r"\b(person|people|pedestrian|man|woman|human|individual|someone)\b", re.I
)

_token_lock = threading.Lock()
_token: str | None = None
_token_at = 0.0


def login(force: bool = False) -> str:
    global _token, _token_at
    with _token_lock:
        if not force and _token and (time.time() - _token_at) < 25 * 60:
            return _token
        r = requests.post(
            f"{VSS_URL}/api/v1/auth/login",
            json={"username": VSS_USERNAME, "password": VSS_PASSWORD},
            timeout=30,
        )
        r.raise_for_status()
        _token = r.json()["access_token"]
        _token_at = time.time()
        return _token


def vss_json(method: str, path: str, **kwargs) -> requests.Response:
    token = login()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    url = f"{VSS_URL}{path}"
    r = requests.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 90), **kwargs)
    if r.status_code == 401:
        token = login(force=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.request(method, url, headers=headers, timeout=kwargs.get("timeout", 90), **kwargs)
    return r


def classify(text: str, similarity: float) -> dict:
    text = text or ""
    has_box = bool(MAILBOX_RE.search(text))
    has_person = bool(PERSON_RE.search(text))
    score = float(similarity or 0)
    if has_box:
        score += 0.12
    if has_person:
        score += 0.08
    if has_box and has_person:
        label = "confirmed"
        score += 0.1
    elif has_box or has_person:
        label = "likely"
    else:
        label = "related"
    return {
        "label": label,
        "has_mailbox": has_box,
        "has_person": has_person,
        "watch_score": round(min(score, 1.0), 4),
    }


def scan(query: str, location: str | None, camera_id: str | None, top_k: int) -> dict:
    body: dict = {
        "query": query or DEFAULT_QUERY,
        "top_k": max(1, min(top_k, 50)),
        "llm_top_n": 4,
        "min_similarity": 0.22,
        "include_public": True,
        "time_filter": "all",
        "system_prompt": (
            "You are a postal-activity analyst. From the video evidence, list who used a "
            "mailbox, where, and what else is in frame. Ignore cars unless they interact "
            "with the mailbox. Be concise."
        ),
    }
    filters = {}
    if location:
        filters["location"] = location
    if camera_id:
        filters["camera_id"] = camera_id
    if filters:
        body["metadata_filters"] = filters

    r = vss_json("POST", "/api/v1/search", json=body)
    r.raise_for_status()
    data = r.json()
    events = []
    for row in data.get("results") or []:
        reasoning = row.get("reasoning_content") or ""
        flags = classify(reasoning, row.get("similarity_score") or 0)
        events.append(
            {
                "source": row.get("source"),
                "original_video": row.get("original_video"),
                "filename": (row.get("original_video") or "").rsplit("/", 1)[-1],
                "camera_id": row.get("camera_id"),
                "location": row.get("location"),
                "capture_type": row.get("capture_type"),
                "similarity": round(float(row.get("similarity_score") or 0), 4),
                "start_sec": row.get("segment_start_sec"),
                "end_sec": row.get("segment_end_sec"),
                "segment_number": row.get("segment_number"),
                "total_segments": row.get("total_segments"),
                "reasoning": reasoning,
                **flags,
            }
        )
    events.sort(key=lambda e: (e["label"] != "confirmed", -e["watch_score"]))
    synth = (data.get("llm_synthesis") or {}).get("response") or ""
    confirmed = sum(1 for e in events if e["label"] == "confirmed")
    return {
        "query": body["query"],
        "briefing": synth,
        "counts": {
            "hits": len(events),
            "confirmed": confirmed,
            "likely": sum(1 for e in events if e["label"] == "likely"),
        },
        "events": events,
    }


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Mailbox Watch</title>
<style>
  :root {
    --ink: #1b2430;
    --paper: #f4efe4;
    --card: #fffdf8;
    --blue: #1d4e89;
    --stamp: #c45c26;
    --line: #d9d0c0;
    --ok: #2f6b3a;
    --mid: #8a5a12;
    --mute: #5c6570;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: "Iowan Old Style", Palatino, "Palatino Linotype", "Times New Roman", serif;
    background: var(--paper);
    color: var(--ink);
    min-height: 100vh;
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    gap: 1rem;
    padding: 1.4rem 1.8rem 1rem;
    border-bottom: 3px double var(--ink);
    background: linear-gradient(180deg, #efe6d4 0%, var(--paper) 100%);
  }
  h1 {
    margin: 0;
    font-size: 1.7rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .sub { color: var(--mute); font-size: 0.95rem; margin-top: 0.2rem; }
  .badge {
    border: 1px solid var(--ink);
    padding: 0.35rem 0.7rem;
    font-size: 0.75rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    background: #fff;
  }
  form {
    display: grid;
    grid-template-columns: 1fr auto auto auto;
    gap: 0.6rem;
    padding: 1rem 1.8rem;
    border-bottom: 1px solid var(--line);
  }
  input, select, button {
    font: inherit;
    padding: 0.55rem 0.7rem;
    border: 1px solid var(--ink);
    background: #fff;
  }
  button {
    background: var(--blue);
    color: #fff;
    cursor: pointer;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    font-size: 0.85rem;
  }
  button:disabled { opacity: 0.55; cursor: wait; }
  .stats {
    display: flex;
    gap: 1.2rem;
    padding: 0.8rem 1.8rem;
    font-variant-numeric: tabular-nums;
  }
  .stats b { color: var(--blue); }
  .briefing {
    margin: 0 1.8rem 1rem;
    padding: 0.9rem 1rem;
    background: var(--card);
    border-left: 6px solid var(--stamp);
    white-space: pre-wrap;
  }
  .layout {
    display: grid;
    grid-template-columns: minmax(280px, 1fr) 1.2fr;
    gap: 1rem;
    padding: 0 1.8rem 2rem;
  }
  .list { display: flex; flex-direction: column; gap: 0.5rem; max-height: 70vh; overflow: auto; }
  .hit {
    background: var(--card);
    border: 1px solid var(--line);
    padding: 0.7rem 0.8rem;
    cursor: pointer;
    text-align: left;
  }
  .hit.active { border-color: var(--blue); box-shadow: 0 0 0 2px var(--blue); }
  .tag {
    display: inline-block;
    font-size: 0.68rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.12rem 0.4rem;
    margin-right: 0.3rem;
    border: 1px solid currentColor;
  }
  .confirmed { color: var(--ok); }
  .likely { color: var(--mid); }
  .related { color: var(--mute); }
  .meta { font-size: 0.82rem; color: var(--mute); margin-top: 0.25rem; }
  .reason { margin: 0.35rem 0 0; font-size: 0.92rem; }
  video {
    width: 100%;
    background: #111;
    border: 1px solid var(--ink);
    aspect-ratio: 16/10;
  }
  .player h2 { margin: 0 0 0.6rem; font-size: 1.05rem; }
  .err { color: #8b1e1e; padding: 0 1.8rem; }
  @media (max-width: 900px) {
    form, .layout { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
  <header>
    <div>
      <h1>Mailbox Watch</h1>
      <div class="sub">Street footage, scored for people using mailboxes</div>
    </div>
    <div class="badge">Postal activity desk</div>
  </header>
  <form id="scan-form">
    <input id="query" name="query" value="person using a mailbox depositing mail on the sidewalk"/>
    <input id="location" name="location" placeholder="location (optional)"/>
    <input id="camera" name="camera_id" placeholder="camera id (optional)"/>
    <button type="submit" id="go">Scan archive</button>
  </form>
  <div id="err" class="err"></div>
  <div class="stats" id="stats"></div>
  <div class="briefing" id="briefing" hidden></div>
  <div class="layout">
    <div class="list" id="list"></div>
    <div class="player">
      <h2 id="now">Select a hit</h2>
      <video id="player" controls playsinline></video>
      <p class="reason" id="now-reason"></p>
    </div>
  </div>
<script>
const form = document.getElementById("scan-form");
const list = document.getElementById("list");
const stats = document.getElementById("stats");
const briefing = document.getElementById("briefing");
const err = document.getElementById("err");
const player = document.getElementById("player");
const now = document.getElementById("now");
const nowReason = document.getElementById("now-reason");
let events = [];

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.textContent = "";
  const btn = document.getElementById("go");
  btn.disabled = true;
  try {
    const q = new URLSearchParams({
      query: document.getElementById("query").value,
      location: document.getElementById("location").value,
      camera_id: document.getElementById("camera").value,
    });
    const r = await fetch("/app/api/scan?" + q.toString());
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    events = data.events || [];
    stats.innerHTML =
      "<span>Hits <b>" + data.counts.hits + "</b></span>" +
      "<span>Confirmed mailbox+person <b>" + data.counts.confirmed + "</b></span>" +
      "<span>Likely <b>" + data.counts.likely + "</b></span>";
    if (data.briefing) {
      briefing.hidden = false;
      briefing.textContent = data.briefing;
    } else {
      briefing.hidden = true;
    }
    renderList();
    if (events[0]) select(0);
  } catch (ex) {
    err.textContent = ex.message;
  } finally {
    btn.disabled = false;
  }
});

function renderList() {
  list.innerHTML = events.map((ev, i) => `
    <button class="hit" data-i="${i}">
      <span class="tag ${ev.label}">${ev.label}</span>
      <strong>${ev.filename || "clip"}</strong>
      <div class="meta">${ev.location || "?"} · ${ev.camera_id || "?"} · ${ev.start_sec}–${ev.end_sec}s · sim ${ev.similarity}</div>
      <p class="reason">${(ev.reasoning || "").slice(0, 220)}</p>
    </button>`).join("");
  list.querySelectorAll(".hit").forEach(el => {
    el.addEventListener("click", () => select(Number(el.dataset.i)));
  });
}

function select(i) {
  document.querySelectorAll(".hit").forEach((el, n) => el.classList.toggle("active", n === i));
  const ev = events[i];
  now.textContent = (ev.filename || "clip") + "  seg " + ev.segment_number + "/" + ev.total_segments;
  nowReason.textContent = ev.reasoning || "";
  player.src = "/app/api/clip?source=" + encodeURIComponent(ev.source);
  player.play().catch(() => {});
}

window.addEventListener("load", () => form.requestSubmit());
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys_stderr = __import__("sys").stderr
        print("%s - %s" % (self.address_string(), fmt % args), file=sys_stderr)

    def _send(self, code: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if path == "/health":
            ok = bool(VSS_URL and VSS_USERNAME and VSS_PASSWORD)
            payload = json.dumps({"ok": ok, "vss": bool(VSS_URL)}).encode()
            return self._send(200 if ok else 503, payload, "application/json")
        if path == "/api/scan":
            try:
                query = (qs.get("query") or [DEFAULT_QUERY])[0]
                location = (qs.get("location") or [""])[0].strip() or None
                camera_id = (qs.get("camera_id") or [""])[0].strip() or None
                result = scan(query, location, camera_id, top_k=24)
                return self._send(200, json.dumps(result).encode(), "application/json")
            except Exception as e:
                return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
        if path == "/api/clip":
            source = (qs.get("source") or [""])[0]
            source = unquote(source)
            if not source.startswith("s3://"):
                return self._send(400, b'{"error":"bad source"}', "application/json")
            return self._proxy_clip(source)
        self._send(404, b"not found", "text/plain")

    def _proxy_clip(self, source: str) -> None:
        token = login()
        url = f"{VSS_URL}/api/v1/videos/stream?source={quote(source, safe='')}&token={quote(token)}"
        headers = {}
        if rng := self.headers.get("Range"):
            headers["Range"] = rng
        r = requests.get(url, headers=headers, stream=True, timeout=120)
        self.send_response(r.status_code)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if r.headers.get(h):
                self.send_header(h, r.headers[h])
        self.end_headers()
        try:
            for chunk in r.iter_content(64 * 1024):
                if chunk:
                    self.wfile.write(chunk)
        except BrokenPipeError:
            pass


def main() -> None:
    if not (VSS_URL and VSS_USERNAME and VSS_PASSWORD):
        raise SystemExit("VSS_URL / VSS_USERNAME / VSS_PASSWORD are required")
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"mailbox-watch on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
