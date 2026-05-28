import hashlib
import hmac
import logging
import os
import platform
import secrets
import shutil
import subprocess
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("producertags")

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
SOUNDS_DIR = Path("sounds")
TEMPLATES_DIR = Path("templates")
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

ALLOWED_AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aiff", ".aif", ".ogg", ".flac"}
EVENT_LOG: deque = deque(maxlen=100)

app = FastAPI()
security = HTTPBasic()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False, default_flow_style=False)


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    if not WEBHOOK_SECRET:
        log.warning("GITHUB_WEBHOOK_SECRET is unset — rejecting request")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    sent = signature_header.split("=", 1)[1]
    mac = hmac.new(WEBHOOK_SECRET, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, mac)


def classify_event(event: str, payload: dict) -> Optional[str]:
    if event == "push":
        if payload.get("deleted") or payload.get("created"):
            return None
        return "push"
    if event == "pull_request":
        action = payload.get("action")
        if action == "opened":
            return "pr_opened"
        if action == "closed" and payload.get("pull_request", {}).get("merged"):
            return "pr_merged"
        return None
    if event == "pull_request_review":
        action = payload.get("action")
        state = payload.get("review", {}).get("state")
        if action == "submitted" and state == "approved":
            return "pr_approved"
        return None
    return None


def extract_actor(event: str, payload: dict) -> str:
    sender = payload.get("sender", {}).get("login")
    if sender:
        return sender
    if event == "push":
        return payload.get("pusher", {}).get("name", "unknown")
    return "unknown"


def resolve_sound(config: dict, internal_event: str, actor: str) -> Optional[Path]:
    event_cfg = config.get("events", {}).get(internal_event, {}) or {}
    authors = event_cfg.get("authors") or {}
    candidates = [authors.get(actor), event_cfg.get("default"), config.get("default_sound")]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
        if c:
            log.warning("Configured sound not found on disk: %s", c)
    return None


def pick_player() -> Optional[list[str]]:
    if platform.system() == "Darwin" and shutil.which("afplay"):
        return ["afplay"]
    for cmd in ("mpg123", "ffplay", "aplay", "paplay"):
        if shutil.which(cmd):
            if cmd == "ffplay":
                return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
            if cmd == "mpg123":
                return ["mpg123", "-q"]
            return [cmd]
    return None


def play_sound(path: Path) -> None:
    player = pick_player()
    if not player:
        log.error("No audio player found (tried afplay, mpg123, ffplay, aplay, paplay)")
        return
    log.info("Playing %s via %s", path, player[0])
    subprocess.Popen(
        [*player, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def record_event(entry: dict) -> None:
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    EVENT_LOG.appendleft(entry)


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    if not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="dashboard password not configured")
    user_ok = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def safe_sound_path(name: str) -> Path:
    p = (SOUNDS_DIR / name).resolve()
    if SOUNDS_DIR.resolve() not in p.parents and p != SOUNDS_DIR.resolve():
        raise HTTPException(status_code=400, detail="invalid path")
    return p


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/")
@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_github_event: Optional[str] = Header(default=None),
) -> dict:
    raw = await request.body()
    if not verify_signature(raw, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    internal_event = classify_event(x_github_event or "", payload)
    if not internal_event:
        record_event({
            "status": "ignored",
            "github_event": x_github_event,
            "action": payload.get("action"),
            "actor": extract_actor(x_github_event or "", payload),
        })
        return {"status": "ignored", "event": x_github_event, "action": payload.get("action")}

    actor = extract_actor(x_github_event or "", payload)
    config = load_config()
    sound = resolve_sound(config, internal_event, actor)
    log.info("event=%s actor=%s sound=%s", internal_event, actor, sound)

    if sound:
        play_sound(sound)
        record_event({"status": "played", "event": internal_event, "actor": actor, "sound": str(sound)})
        return {"status": "played", "event": internal_event, "actor": actor, "sound": str(sound)}

    record_event({"status": "no_sound", "event": internal_event, "actor": actor, "sound": None})
    return {"status": "no_sound", "event": internal_event, "actor": actor}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(_: str = Depends(require_auth)) -> HTMLResponse:
    html = (TEMPLATES_DIR / "dashboard.html").read_text()
    return HTMLResponse(html)


@app.get("/api/config")
def api_get_config(_: str = Depends(require_auth)) -> JSONResponse:
    return JSONResponse(load_config())


@app.post("/api/config")
async def api_save_config(request: Request, _: str = Depends(require_auth)) -> dict:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="config must be an object")
    save_config(body)
    return {"status": "saved"}


@app.get("/api/sounds")
def api_list_sounds(_: str = Depends(require_auth)) -> dict:
    files = []
    if SOUNDS_DIR.exists():
        for p in sorted(SOUNDS_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() in ALLOWED_AUDIO_EXT:
                files.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
    return {"sounds": files}


@app.post("/api/sounds")
async def api_upload_sound(
    file: UploadFile = File(...),
    _: str = Depends(require_auth),
) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="no filename")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_AUDIO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported extension; allowed: {sorted(ALLOWED_AUDIO_EXT)}",
        )
    SOUNDS_DIR.mkdir(exist_ok=True)
    safe_name = Path(file.filename).name
    dest = safe_sound_path(safe_name)
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
    return {"status": "uploaded", "name": safe_name, "path": str(dest.relative_to(Path.cwd()))}


@app.delete("/api/sounds/{name}")
def api_delete_sound(name: str, _: str = Depends(require_auth)) -> dict:
    p = safe_sound_path(name)
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    p.unlink()
    return {"status": "deleted", "name": name}


@app.post("/api/play")
async def api_play(request: Request, _: str = Depends(require_auth)) -> dict:
    body = await request.json()
    name = body.get("name") if isinstance(body, dict) else None
    if not name:
        raise HTTPException(status_code=400, detail="missing 'name'")
    p = safe_sound_path(name)
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    play_sound(p)
    return {"status": "playing", "name": name}


@app.get("/api/events")
def api_events(_: str = Depends(require_auth)) -> dict:
    return {"events": list(EVENT_LOG)}
