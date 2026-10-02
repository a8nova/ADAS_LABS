#!/usr/bin/env python3
"""Carrier Route — drop-offs vs walk-bys, mapped, then the loop to hit."""

from __future__ import annotations

import hashlib
import itertools
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

SERVE_RE = re.compile(
    r"backpack.{0,80}using a blue mailbox on the sidewalk|"
    r"using a blue mailbox on the sidewalk.{0,80}backpack",
    re.I,
)
WALK_RE = re.compile(r"walks past the mailbox|walking past the mailbox|walks past a blue mailbox", re.I)
BOX_RE = re.compile(r"mail\s*box", re.I)

CAM_XY = {
    "sf_streets_cam-1": (2.2, 1.4),
    "sf_streets_cam-2": (3.6, 1.1),
    "sf_streets_cam-3": (2.8, 2.6),
    "sf_streets_cam-4": (4.3, 1.9),
    "neighborhood_cam-1": (1.2, 3.3),
    "pie_cam-3": (6.1, 5.4),
}
CAM_STREET = {
    "sf_streets_cam-1": "Cam 1 · sidewalk",
    "sf_streets_cam-2": "Cam 2 · sidewalk",
    "sf_streets_cam-3": "Cam 3 · sidewalk",
    "sf_streets_cam-4": "Cam 4 · sidewalk",
    "neighborhood_cam-1": "Neighborhood block",
    "pie_cam-3": "Toronto block",
}
DEPOT_XY = (0.35, 0.55)
MIN_PER_UNIT = 3.2
WALK_CAP = 7

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
    timeout = kwargs.pop("timeout", 90)
    url = f"{VSS_URL}{path}"
    r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    if r.status_code == 401:
        token = login(force=True)
        headers["Authorization"] = f"Bearer {token}"
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    return r


def decide(text: str) -> tuple[str, str]:
    text = text or ""
    if SERVE_RE.search(text):
        return "Serve", "using a blue mailbox on the sidewalk"
    if BOX_RE.search(text) and WALK_RE.search(text) and not SERVE_RE.search(text):
        return "Pass", "walked past — no drop-off"
    return "ignore", ""


def iter_indexed_clips(location: str | None):
    """Read captions already in VastDB via Explore — no Embed1 / hybrid search."""
    offset = 0
    while True:
        r = vss_json(
            "GET",
            f"/api/v1/videos/explore?scope=all&limit=100&offset={offset}",
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        chunks = data.get("chunks") or []
        for chunk in chunks:
            if location and (chunk.get("location") or "") != location:
                continue
            parent = chunk.get("original_video")
            cam = chunk.get("camera_id")
            loc = chunk.get("location")
            for t in chunk.get("timeline") or []:
                yield {
                    "source": t.get("source"),
                    "reasoning_content": t.get("reasoning_content"),
                    "segment_number": t.get("segment_number"),
                    "segment_start_sec": t.get("segment_start_sec"),
                    "segment_end_sec": t.get("segment_end_sec"),
                    "camera_id": cam,
                    "location": loc,
                    "original_video": parent,
                    "similarity_score": t.get("similarity_score") or 0,
                }
        offset += 100
        total = data.get("total") or 0
        if not chunks or offset >= total:
            break


def pin_xy(camera_id: str, source: str) -> tuple[float, float]:
    bx, by = CAM_XY.get(camera_id or "", (3.0, 2.0))
    h = int(hashlib.md5(source.encode()).hexdigest(), 16)
    ox = ((h % 17) - 8) * 0.08
    oy = (((h // 17) % 17) - 8) * 0.07
    return round(bx + ox, 3), round(by + oy, 3)


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def drive_min(path: list[tuple[float, float]]) -> float:
    if len(path) < 2:
        return 0.0
    return round(sum(dist(path[i], path[i + 1]) for i in range(len(path) - 1)) * MIN_PER_UNIT, 1)


def best_loop(serves: list[dict]) -> dict:
    if not serves:
        return {"drive": "Office → Office", "minutes": 0, "stops": 0, "order": [], "pick": "No drop-offs"}
    pts = serves[:7]
    names = list(range(len(pts)))
    best_perm = names
    best_m = 1e9
    for perm in itertools.permutations(names):
        path = [DEPOT_XY] + [(pts[i]["x"], pts[i]["y"]) for i in perm] + [DEPOT_XY]
        m = drive_min(path)
        if m < best_m:
            best_m = m
            best_perm = perm
    labels = [pts[i]["label"] for i in best_perm]
    drive = " → ".join(["Office"] + labels + ["Office"])
    return {
        "drive": drive,
        "minutes": best_m,
        "stops": len(pts),
        "order": [pts[i]["id"] for i in best_perm],
        "pick": "Use this",
    }


def stop_from_row(row: dict) -> dict | None:
    src = row.get("source")
    reasoning = row.get("reasoning_content") or ""
    decision, signal = decide(reasoning)
    if not src or decision == "ignore":
        return None
    cam = row.get("camera_id") or "unknown"
    x, y = pin_xy(cam, src)
    parent = (row.get("original_video") or "").rsplit("/", 1)[-1]
    return {
        "id": src,
        "label": "",
        "house": f"clip {row.get('segment_number') or '?'}",
        "street": CAM_STREET.get(cam, cam),
        "camera_id": cam,
        "location": row.get("location") or "",
        "filename": parent,
        "start_sec": row.get("segment_start_sec"),
        "end_sec": row.get("segment_end_sec"),
        "source": src,
        "reasoning": reasoning,
        "signal": signal,
        "decision": decision,
        "similarity": round(float(row.get("similarity_score") or 0), 4),
        "x": x,
        "y": y,
    }


def label_stops(stops: list[dict]) -> None:
    n = 1
    for s in stops:
        if s["decision"] == "Serve":
            s["label"] = f"Box {n}"
            n += 1
    for s in stops:
        if s["decision"] == "Pass":
            s["label"] = f"Box {n}"
            n += 1


def build_route(area: str, start: str, end: str) -> dict:
    location = area if area and area != "all" else None
    rows: dict[str, dict] = {}
    walk_hits = []
    for row in iter_indexed_clips(location):
        stop = stop_from_row(row)
        if not stop:
            continue
        if stop["decision"] == "Serve":
            rows[stop["id"]] = stop
        elif stop["decision"] == "Pass":
            walk_hits.append(stop)
    walk_hits.sort(key=lambda s: -s["similarity"])
    for stop in walk_hits[:WALK_CAP]:
        if stop["id"] not in rows:
            rows[stop["id"]] = stop

    stops = sorted(rows.values(), key=lambda s: (s["decision"] != "Serve", -s["similarity"]))
    label_stops(stops)
    serves = [s for s in stops if s["decision"] == "Serve"]
    passes = [s for s in stops if s["decision"] == "Pass"]
    loop = best_loop(serves)
    if passes:
        via_all = drive_min(
            [DEPOT_XY] + [(s["x"], s["y"]) for s in serves + passes] + [DEPOT_XY]
        )
        saved = max(round(via_all - loop["minutes"], 1), 0)
    else:
        saved = 0.0

    return {
        "start": start or "Main office",
        "end": end or "Main office",
        "area": location or "all",
        "depot": {"x": DEPOT_XY[0], "y": DEPOT_XY[1], "label": start or "Main office"},
        "stats": {
            "serve": len(serves),
            "pass": len(passes),
            "saved_min": saved,
        },
        "stops": stops,
        "orders": [loop],
        "loop": loop["drive"],
        "route_ids": loop["order"],
    }


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Carrier Route</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=Fraunces:opsz,wght@9..144,500;9..144,650;9..144,700&display=swap" rel="stylesheet"/>
<style>
  :root {
    --ink: #122033;
    --paper: #f3ecde;
    --card: #fffdf8;
    --line: #e2d8c4;
    --navy: #163a66;
    --navy-2: #1e4e86;
    --ok: #1a7a48;
    --ok-soft: #e8f6ee;
    --pass: #b56a12;
    --pass-soft: #f8eedf;
    --mute: #667085;
    --shadow: 0 18px 40px rgba(18, 32, 51, .08);
  }
  * { box-sizing: border-box; }
  html, body { min-height: 100%; }
  body {
    margin: 0;
    color: var(--ink);
    font-family: "DM Sans", system-ui, sans-serif;
    background:
      radial-gradient(900px 420px at 8% -8%, #fff8ea 0%, transparent 55%),
      radial-gradient(700px 380px at 100% 0%, #dce8f5 0%, transparent 46%),
      linear-gradient(180deg, #f7f1e4 0%, var(--paper) 40%);
  }
  .shell { max-width: 1180px; margin: 0 auto; padding: 1.1rem 1.25rem 2.4rem; }
  header {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    padding: 1.05rem 1.2rem; background: var(--card); border: 1px solid var(--line);
    border-radius: 18px; box-shadow: var(--shadow);
  }
  .brand { display: flex; gap: .85rem; align-items: center; }
  .mark {
    width: 46px; height: 46px; border-radius: 14px; flex: none;
    background: linear-gradient(145deg, var(--navy), var(--navy-2));
    color: #fff; display: grid; place-items: center; font-family: Fraunces, serif;
    font-weight: 700; letter-spacing: .04em; box-shadow: inset 0 0 0 2px rgba(255,255,255,.18);
  }
  h1 { margin: 0; font-family: Fraunces, serif; font-size: 1.55rem; letter-spacing: -.02em; }
  .lede { margin: .18rem 0 0; color: var(--mute); font-size: .92rem; max-width: 36rem; line-height: 1.4; }
  .stamp {
    border: 1.5px dashed var(--navy); color: var(--navy); background: #f4f8fd;
    padding: .42rem .7rem; border-radius: 999px; font-size: .68rem;
    letter-spacing: .16em; text-transform: uppercase; font-weight: 700; white-space: nowrap;
  }
  .nav { display: flex; gap: .4rem; margin: 1rem 0 .85rem; padding: .35rem; background: rgba(255,255,255,.65);
    border: 1px solid var(--line); border-radius: 14px; width: fit-content; }
  .nav button {
    font: inherit; cursor: pointer; border: 0; background: transparent; color: var(--mute);
    padding: .5rem .9rem; border-radius: 10px; font-weight: 600; letter-spacing: .01em;
  }
  .nav button.on { background: var(--navy); color: #fff; box-shadow: 0 8px 16px rgba(22,58,102,.22); }
  .stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: .7rem; margin-bottom: 1rem; }
  .stat {
    background: var(--card); border: 1px solid var(--line); border-radius: 16px;
    padding: .9rem 1rem; box-shadow: var(--shadow); min-height: 5.2rem;
  }
  .stat b { display: block; font-family: Fraunces, serif; font-size: 1.7rem; line-height: 1.05; }
  .stat span { color: var(--mute); font-size: .78rem; font-weight: 500; }
  .stat.ok { background: linear-gradient(180deg, #fff, var(--ok-soft)); }
  .stat.ok b { color: var(--ok); }
  .stat.warn { background: linear-gradient(180deg, #fff, var(--pass-soft)); }
  .stat.warn b { color: var(--pass); }
  section { display: none; }
  section.on { display: block; animation: rise .28s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .panel {
    background: var(--card); border: 1px solid var(--line); border-radius: 18px;
    padding: 1.25rem 1.35rem 1.4rem; box-shadow: var(--shadow);
  }
  .kicker { margin: 0 0 .35rem; color: var(--navy); font-size: .72rem; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; }
  .panel h2 { margin: 0 0 .45rem; font-family: Fraunces, serif; font-size: 1.35rem; }
  .panel p.intro { margin: 0 0 1.1rem; color: var(--mute); max-width: 42rem; line-height: 1.5; }
  .form { display: grid; grid-template-columns: 1fr 1fr; gap: .85rem 1rem; max-width: 42rem; }
  .form .full { grid-column: 1 / -1; }
  label { display: block; margin-bottom: .28rem; font-size: .74rem; color: var(--mute); font-weight: 600; letter-spacing: .04em; text-transform: uppercase; }
  input, select {
    width: 100%; padding: .7rem .8rem; font: inherit; border: 1px solid #d7cebb;
    background: #fff; border-radius: 12px; color: var(--ink);
  }
  input:focus, select:focus { outline: 2px solid #c5d8ef; border-color: var(--navy-2); }
  .primary {
    margin-top: .35rem; padding: .78rem 1.15rem; border: 0; border-radius: 12px; cursor: pointer;
    background: linear-gradient(180deg, var(--navy-2), var(--navy)); color: #fff;
    font: inherit; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
    box-shadow: 0 10px 22px rgba(22,58,102,.24);
  }
  .primary:disabled { opacity: .55; cursor: wait; }
  .split { display: grid; grid-template-columns: 1.08fr .92fr; gap: 1rem; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 1rem; box-shadow: var(--shadow); }
  .map-wrap { background: #d7e4ef; border: 1px solid var(--line); border-radius: 18px; overflow: hidden; box-shadow: var(--shadow); position: relative; }
  .map-cap { position: absolute; top: .7rem; left: .8rem; background: rgba(255,253,248,.92); border-radius: 999px; padding: .28rem .65rem; font-size: .72rem; font-weight: 600; color: var(--navy); }
  svg.map { width: 100%; height: auto; display: block; }
  .legend { display: flex; gap: 1rem; flex-wrap: wrap; font-size: .8rem; color: var(--mute); margin: .7rem 0 0; font-weight: 500; }
  .sw { display: inline-block; width: .72rem; height: .72rem; border-radius: 50%; margin-right: .32rem; vertical-align: -1px; }
  .list { max-height: 56vh; overflow: auto; margin-top: .55rem; }
  .row {
    display: grid; grid-template-columns: 4.6rem 1fr auto; gap: .65rem; align-items: start;
    padding: .75rem .65rem; border-radius: 12px; cursor: pointer; border: 1px solid transparent;
  }
  .row + .row { margin-top: .2rem; }
  .row:hover { background: #f7f2e8; }
  .row.on { background: #eef4fb; border-color: #c9dcec; }
  .tag { display: inline-block; font-size: .64rem; letter-spacing: .1em; text-transform: uppercase; font-weight: 700; border-radius: 999px; padding: .18rem .45rem; }
  .tag.serve { color: var(--ok); background: var(--ok-soft); }
  .tag.pass { color: var(--pass); background: var(--pass-soft); }
  .row strong { display: block; margin-top: .28rem; font-size: .92rem; }
  .sig { color: var(--mute); font-size: .8rem; margin: .18rem 0 0; line-height: 1.4; }
  .player-head { display: flex; justify-content: space-between; align-items: baseline; gap: .5rem; margin-bottom: .55rem; }
  .player-head h3 { margin: 0; font-family: Fraunces, serif; font-size: 1.12rem; }
  video { width: 100%; background: #10141c; border-radius: 14px; aspect-ratio: 16/10; display: block; }
  .caption { margin: .7rem 0 0; padding: .75rem .8rem; background: #f6f1e6; border-radius: 12px; color: #3d4754; font-size: .86rem; line-height: 1.45; }
  .loop {
    font-family: Fraunces, serif; font-size: 1.28rem; margin: 0 0 .9rem;
    padding: .95rem 1.1rem; background: var(--card); border: 1px solid var(--line);
    border-radius: 16px; box-shadow: var(--shadow);
  }
  .sheet-card h3 { margin: .15rem 0 .55rem; font-family: Fraunces, serif; font-size: 1.05rem; }
  ol#hit-list, ul#pass-list { margin: 0; padding-left: 1.15rem; }
  ol#hit-list li { margin: 0 0 .55rem; }
  ul#pass-list { color: var(--mute); }
  .err { color: #9b1c1c; background: #fdecec; border-radius: 12px; padding: .65rem .85rem; margin: 0 0 .85rem; display: none; }
  .err.on { display: block; }
  .empty { color: var(--mute); padding: 1.4rem .4rem; text-align: center; }
  @media (max-width: 900px) {
    .split, .form, .stats { grid-template-columns: 1fr; }
    header { flex-wrap: wrap; }
    .nav { width: 100%; }
  }
</style>
</head>
<body>
  <div class="shell">
    <header>
      <div class="brand">
        <div class="mark">CR</div>
        <div>
          <h1>Carrier Route</h1>
          <p class="lede">Street video decides who used the box. The map keeps drop-offs and skips the walk-bys.</p>
        </div>
      </div>
      <div class="stamp">Collection desk</div>
    </header>

    <nav class="nav">
      <button type="button" data-screen="shift" class="on">1 · Shift</button>
      <button type="button" data-screen="board">2 · Evidence</button>
      <button type="button" data-screen="sheet">3 · Route</button>
    </nav>

    <div class="stats" id="stats">
      <div class="stat"><b>—</b><span>Drop-offs to hit</span></div>
      <div class="stat"><b>—</b><span>Walked by — skip</span></div>
      <div class="stat"><b>—</b><span>Minutes not spent on pass-bys</span></div>
    </div>
    <div class="err" id="err"></div>

    <section id="shift" class="on">
      <div class="panel">
        <p class="kicker">Dispatch</p>
        <h2>Set the desk, then build the loop</h2>
        <p class="intro">Serves are people using a blue mailbox with a backpack. Pass-bys walked past and never dropped mail — those boxes stay off the route.</p>
        <div class="form">
          <div><label>Start</label><input id="start" value="Main office"/></div>
          <div><label>End</label><input id="end" value="Main office"/></div>
          <div class="full">
            <label>Area</label>
            <select id="area">
              <option value="san_francisco">San Francisco streets</option>
              <option value="neighborhood">Neighborhood</option>
              <option value="toronto">Toronto streets</option>
              <option value="all">All indexed cameras</option>
            </select>
          </div>
          <div class="full"><button class="primary" id="build" type="button">Build route</button></div>
        </div>
      </div>
    </section>

    <section id="board">
      <div class="split">
        <div class="card">
          <p class="kicker">Indexed captions</p>
          <div class="legend">
            <span><i class="sw" style="background:var(--ok)"></i>Hit — drop-off</span>
            <span><i class="sw" style="background:var(--pass)"></i>Close — walked by</span>
          </div>
          <div class="list" id="board-list"></div>
        </div>
        <div class="card">
          <div class="player-head">
            <h3 id="now">Select a mailbox</h3>
          </div>
          <video id="player" controls playsinline></video>
          <p class="caption" id="now-reason">Open a clip on the left to see the model caption that classified it.</p>
        </div>
      </div>
    </section>

    <section id="sheet">
      <p class="loop" id="loop">Build a route to draw the loop home.</p>
      <div class="split">
        <div class="map-wrap" id="map-card">
          <div class="map-cap">Schematic pins · not GPS</div>
          <svg class="map" id="map" viewBox="0 0 720 400" role="img" aria-label="Mailbox map and route"></svg>
        </div>
        <div class="card sheet-card">
          <h3>Boxes to hit</h3>
          <ol id="hit-list"></ol>
          <h3 style="margin-top:1.1rem">Walked by — skip</h3>
          <ul id="pass-list"></ul>
        </div>
      </div>
      <p class="legend">
        <span><i class="sw" style="background:var(--ok)"></i>Route stop</span>
        <span><i class="sw" style="background:var(--pass)"></i>No stop</span>
        <span><i class="sw" style="background:#122033"></i>Office</span>
      </p>
    </section>
  </div>

<script>
const API = "/app/";
let data = {stops:[], orders:[], stats:{}, depot:{x:0.35,y:0.55,label:"Office"}};

document.querySelectorAll(".nav button").forEach(b => b.addEventListener("click", () => show(b.dataset.screen)));
function show(name) {
  document.querySelectorAll(".nav button").forEach(b => b.classList.toggle("on", b.dataset.screen === name));
  document.querySelectorAll("section").forEach(s => s.classList.toggle("on", s.id === name));
  if (name === "sheet") drawMap();
}

document.getElementById("build").addEventListener("click", async () => {
  const err = document.getElementById("err");
  const btn = document.getElementById("build");
  err.classList.remove("on");
  err.textContent = "";
  btn.disabled = true;
  btn.textContent = "Reading captions…";
  try {
    const q = new URLSearchParams({
      start: document.getElementById("start").value,
      end: document.getElementById("end").value,
      area: document.getElementById("area").value,
    });
    const r = await fetch(API + "api/route?" + q.toString());
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.statusText);
    data = body;
    renderAll();
    show("board");
  } catch (e) {
    err.textContent = e.message;
    err.classList.add("on");
  } finally {
    btn.disabled = false;
    btn.textContent = "Build route";
  }
});

function renderAll() {
  const st = data.stats || {};
  document.getElementById("stats").innerHTML =
    `<div class="stat ok"><b>${st.serve || 0}</b><span>Drop-offs to hit</span></div>` +
    `<div class="stat warn"><b>${st.pass || 0}</b><span>Walked by — skip</span></div>` +
    `<div class="stat"><b>${st.saved_min || 0} min</b><span>Not spent on pass-bys</span></div>`;

  const stops = data.stops || [];
  document.getElementById("board-list").innerHTML = stops.length ? stops.map((s, i) => `
    <div class="row" data-i="${i}">
      <div>
        <span class="tag ${s.decision === "Serve" ? "serve" : "pass"}">${s.decision === "Serve" ? "Hit" : "Close"}</span>
        <strong>${s.label}</strong>
      </div>
      <div>${s.street}<div class="sig">${s.signal}</div><div class="sig">${(s.reasoning || "").slice(0, 140)}${(s.reasoning||"").length>140?"…":""}</div></div>
      <div class="sig">${prettyLoc(s.location)}</div>
    </div>`).join("") : `<div class="empty">No mailbox moments in this area yet.</div>`;
  document.querySelectorAll(".row").forEach(el => el.addEventListener("click", () => select(Number(el.dataset.i))));

  const serves = stops.filter(s => s.decision === "Serve");
  const passes = stops.filter(s => s.decision === "Pass");
  document.getElementById("loop").textContent = data.loop || "No drop-offs on this loop.";
  document.getElementById("hit-list").innerHTML = serves.map(s =>
    `<li><strong>${s.label}</strong> — ${s.street}<div class="sig">${s.signal}</div></li>`).join("") || "<li>None</li>";
  document.getElementById("pass-list").innerHTML = passes.map(s =>
    `<li>${s.label} · ${s.signal}</li>`).join("") || "<li>None in this area</li>";
  drawMap();
}

function prettyLoc(loc) {
  return (loc || "").replace(/_/g, " ");
}

function select(i) {
  document.querySelectorAll(".row").forEach((el, n) => el.classList.toggle("on", n === i));
  const s = data.stops[i];
  document.getElementById("now").textContent = (s.label || "") + " · " + s.street;
  document.getElementById("now-reason").textContent = s.reasoning || "";
  document.getElementById("player").src = API + "api/clip?source=" + encodeURIComponent(s.source);
}

function xy(p) {
  const xs = [data.depot.x, ...(data.stops || []).map(s => s.x)];
  const ys = [data.depot.y, ...(data.stops || []).map(s => s.y)];
  const minx = Math.min(...xs) - 0.4, maxx = Math.max(...xs) + 0.4;
  const miny = Math.min(...ys) - 0.4, maxy = Math.max(...ys) + 0.4;
  const x = 50 + (p.x - minx) / (maxx - minx || 1) * 620;
  const y = 48 + (p.y - miny) / (maxy - miny || 1) * 310;
  return [x, y];
}

function drawMap() {
  const svg = document.getElementById("map");
  if (!svg) return;
  const depot = data.depot || {x:0.35,y:0.55,label:"Office"};
  const serves = (data.stops || []).filter(s => s.decision === "Serve");
  const passes = (data.stops || []).filter(s => s.decision === "Pass");
  const order = data.route_ids || serves.map(s => s.id);
  const ordered = order.map(id => serves.find(s => s.id === id)).filter(Boolean);
  const pts = [depot, ...ordered, depot];
  const line = pts.map(p => xy(p).join(",")).join(" ");
  let html = `<defs>
      <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#eaf2f8"/><stop offset="100%" stop-color="#cfdce8"/>
      </linearGradient>
    </defs>
    <rect width="720" height="400" fill="url(#g)"/>
    <g stroke="#b7c9d8" stroke-width="10" stroke-linecap="round">
      <line x1="36" y1="96" x2="684" y2="96"/><line x1="36" y1="196" x2="684" y2="196"/>
      <line x1="36" y1="296" x2="684" y2="296"/><line x1="168" y1="36" x2="168" y2="364"/>
      <line x1="360" y1="36" x2="360" y2="364"/><line x1="552" y1="36" x2="552" y2="364"/>
    </g>`;
  if (ordered.length) html += `<polyline fill="none" stroke="#1a7a48" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" points="${line}"/>`;
  const [dx, dy] = xy(depot);
  html += `<rect x="${dx-22}" y="${dy-13}" rx="6" width="44" height="26" fill="#122033"/>
    <text x="${dx}" y="${dy+5}" text-anchor="middle" fill="#fff" font-size="9" font-family="DM Sans, sans-serif">OFFICE</text>`;
  for (const s of passes) {
    const [x,y] = xy(s);
    html += `<circle cx="${x}" cy="${y}" r="10" fill="#fffdf8" stroke="#b56a12" stroke-width="2"/>
      <text x="${x}" y="${y+4}" text-anchor="middle" font-size="10" fill="#b56a12">×</text>`;
  }
  for (const s of ordered) {
    const [x,y] = xy(s);
    const n = s.label.replace("Box ","");
    html += `<circle cx="${x}" cy="${y}" r="13" fill="#1a7a48" stroke="#122033" stroke-width="1.5"/>
      <text x="${x}" y="${y+4}" text-anchor="middle" fill="#fff" font-size="11" font-family="DM Sans, sans-serif">${n}</text>`;
  }
  svg.innerHTML = html;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
            return self._send(200 if ok else 503, json.dumps({"ok": ok}).encode(), "application/json")
        if path == "/api/route":
            try:
                area = (qs.get("area") or ["san_francisco"])[0]
                start = (qs.get("start") or ["Main office"])[0]
                end = (qs.get("end") or ["Main office"])[0]
                result = build_route(area, start, end)
                return self._send(200, json.dumps(result).encode(), "application/json")
            except Exception as e:
                return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
        if path == "/api/clip":
            source = unquote((qs.get("source") or [""])[0])
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
    print(f"carrier-route on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
