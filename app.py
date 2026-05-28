import hashlib
import hmac
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, Header, HTTPException, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("producertags")

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.yaml"))
WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "").encode()

app = FastAPI()


def load_config() -> dict:
    with CONFIG_PATH.open() as f:
        return yaml.safe_load(f) or {}


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
        # GitHub sends a push event for branch creation/deletion too; filter those out.
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
    candidates = [
        authors.get(actor),
        event_cfg.get("default"),
        config.get("default_sound"),
    ]
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
    # Fire-and-forget — don't block the webhook response.
    subprocess.Popen(
        [*player, str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


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
        return {"status": "ignored", "event": x_github_event, "action": payload.get("action")}

    actor = extract_actor(x_github_event or "", payload)
    config = load_config()
    sound = resolve_sound(config, internal_event, actor)

    log.info("event=%s actor=%s sound=%s", internal_event, actor, sound)
    if sound:
        play_sound(sound)
        return {"status": "played", "event": internal_event, "actor": actor, "sound": str(sound)}
    return {"status": "no_sound", "event": internal_event, "actor": actor}
