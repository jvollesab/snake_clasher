"""Server-authoritative multiplayer Snake Arena."""

import asyncio
import json
import logging
import random
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

GRID_WIDTH, GRID_HEIGHT = 36, 24
TICK_SECONDS = 0.15
SPAWN_DELAY = 3.0
COLORS = ["#4f8f23", "#e59b23", "#8b4d99", "#277b9e", "#bf4242", "#6f7825"]
Point = Tuple[int, int]

# Hardening limits.
MAX_PLAYERS = 8                 # concurrent snakes in the arena
MAX_MESSAGE_BYTES = 512         # largest client message we will parse
MAX_MESSAGES_PER_SECOND = 30    # per-connection input rate limit
JOIN_TIMEOUT = 15.0             # seconds a socket may idle before joining
SEND_TIMEOUT = 1.0              # seconds before a slow client is dropped
WS_MAX_SIZE = 4096              # transport-level frame cap (uvicorn --ws-max-size)
DIRECTIONS: Dict[str, Point] = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("snake_clash")


@dataclass
class Player:
    name: str
    color: str
    snake: List[Point] = field(default_factory=list)
    direction: Point = (1, 0)
    next_direction: Point = (1, 0)
    score: int = 0
    alive: bool = False
    respawn_at: float = 0.0


class Arena:
    def __init__(self) -> None:
        self.players: Dict[str, Player] = {}
        self.clients: Dict[str, WebSocket] = {}
        self.food: Point = (GRID_WIDTH // 2, GRID_HEIGHT // 2)
        self.round_ends_at: Optional[float] = None
        self.lock = asyncio.Lock()

    def occupied(self) -> Set[Point]:
        return {cell for player in self.players.values() for cell in player.snake if player.alive}

    def spawn_food(self) -> None:
        taken = self.occupied()
        free = [(x, y) for x in range(GRID_WIDTH) for y in range(GRID_HEIGHT) if (x, y) not in taken]
        self.food = random.choice(free) if free else (GRID_WIDTH // 2, GRID_HEIGHT // 2)

    def spawn_player(self, player: Player) -> None:
        taken = self.occupied() | {self.food}
        for _ in range(100):
            x, y = random.randrange(3, GRID_WIDTH - 3), random.randrange(3, GRID_HEIGHT - 3)
            direction = random.choice(list(DIRECTIONS.values()))
            tail = [(x - direction[0] * i, y - direction[1] * i) for i in range(4)]
            if all(0 <= a < GRID_WIDTH and 0 <= b < GRID_HEIGHT for a, b in tail) and not (set(tail) & taken):
                player.snake = tail
                player.direction = player.next_direction = direction
                player.alive = True
                return
        player.respawn_at = time.monotonic() + 1

    def state(self) -> dict:
        players = [
            {"id": key, "name": player.name, "color": player.color, "snake": player.snake,
             "score": player.score, "alive": player.alive}
            for key, player in self.players.items()
        ]
        players.sort(key=lambda item: item["score"], reverse=True)
        restart_in = max(0, (self.round_ends_at or 0) - time.monotonic())
        return {"type": "state", "width": GRID_WIDTH, "height": GRID_HEIGHT, "food": self.food, "players": players,
                "roundOver": self.round_ends_at is not None, "restartIn": round(restart_in, 1)}

    def start_new_round(self) -> None:
        self.round_ends_at = None
        for player in self.players.values():
            player.score = 0
            player.snake = []
            player.alive = False
        for player in self.players.values():
            self.spawn_player(player)
        self.spawn_food()

    def remove(self, player_id: str) -> Optional[WebSocket]:
        self.players.pop(player_id, None)
        return self.clients.pop(player_id, None)

    async def broadcast(self) -> None:
        """Send the current state to every client concurrently.

        The snapshot is taken under the lock, but sending happens outside it so a
        slow or stalled client can never freeze the game loop for everyone else.
        """
        async with self.lock:
            payload = json.dumps(self.state(), separators=(",", ":"))
            targets = list(self.clients.items())
        if not targets:
            return

        async def send(socket: WebSocket) -> None:
            await asyncio.wait_for(socket.send_text(payload), timeout=SEND_TIMEOUT)

        results = await asyncio.gather(*(send(sock) for _, sock in targets), return_exceptions=True)
        stale = [(pid, sock) for (pid, sock), result in zip(targets, results) if isinstance(result, BaseException)]
        if not stale:
            return
        async with self.lock:
            for player_id, _ in stale:
                self.remove(player_id)
        for _, socket in stale:
            try:
                await socket.close(code=1011)
            except Exception:
                pass

    async def tick(self) -> None:
        async with self.lock:
            self._advance(time.monotonic())
        await self.broadcast()

    def _advance(self, now: float) -> None:
        if self.round_ends_at is not None:
            if now >= self.round_ends_at:
                self.start_new_round()
            return
        for player in self.players.values():
            if not player.alive and now >= player.respawn_at:
                self.spawn_player(player)

        moving = {key: player for key, player in self.players.items() if player.alive and player.snake}
        next_heads: Dict[str, Point] = {}
        for key, player in moving.items():
            if (player.next_direction[0] != -player.direction[0] or player.next_direction[1] != -player.direction[1]):
                player.direction = player.next_direction
            head_x, head_y = player.snake[0]
            dx, dy = player.direction
            next_heads[key] = (head_x + dx, head_y + dy)

        bodies = {cell for player in moving.values() for cell in player.snake}
        head_counts = Counter(next_heads.values())
        dead = {
            key for key, (x, y) in next_heads.items()
            if not (0 <= x < GRID_WIDTH and 0 <= y < GRID_HEIGHT) or (x, y) in bodies or head_counts[(x, y)] > 1
        }

        if dead:
            for player in self.players.values():
                player.alive = False
                player.snake = []
            self.round_ends_at = now + SPAWN_DELAY
            return

        ate = False
        for key, player in moving.items():
            head = next_heads[key]
            player.snake.insert(0, head)
            if head == self.food:
                player.score += 10
                ate = True
            else:
                player.snake.pop()
        if ate:
            self.spawn_food()


arena = Arena()


async def game_loop() -> None:
    while True:
        started = time.monotonic()
        try:
            await arena.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad tick kill the game permanently.
            logger.exception("Game tick failed; continuing")
        await asyncio.sleep(max(0, TICK_SECONDS - (time.monotonic() - started)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(game_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# Interactive API docs are not needed for a game and would pull scripts from a CDN.
app = FastAPI(title="Snake Arena", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


def origin_allowed(websocket: WebSocket) -> bool:
    """Block cross-site WebSocket hijacking: browsers must connect from this same host."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # Non-browser clients don't send Origin; CSWSH only applies to browsers.
    host = websocket.headers.get("host", "")
    try:
        return bool(host) and urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


async def receive_message(websocket: WebSocket) -> Optional[dict]:
    """Receive one small JSON object; return None for anything malformed."""
    text = await websocket.receive_text()
    if len(text) > MAX_MESSAGE_BYTES:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def clean_name(raw: object) -> str:
    if not isinstance(raw, str):
        return "Pilot"
    name = "".join(char for char in raw[:64] if char.isalnum() or char in " _-")
    return " ".join(name.split())[:18] or "Pilot"


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    player_id: Optional[str] = None
    try:
        join = await asyncio.wait_for(receive_message(websocket), timeout=JOIN_TIMEOUT)
        if not join or join.get("type") != "join":
            await websocket.close(code=1008)
            return
        async with arena.lock:
            if len(arena.players) >= MAX_PLAYERS:
                full = True
            else:
                full = False
                player_id = uuid.uuid4().hex
                player = Player(name=clean_name(join.get("name")), color=COLORS[len(arena.players) % len(COLORS)])
                arena.players[player_id] = player
                arena.clients[player_id] = websocket
                arena.spawn_player(player)
        if full:
            await websocket.close(code=1013, reason="Arena full")
            return
        await websocket.send_json({"type": "welcome", "id": player_id})

        window_start, count = time.monotonic(), 0
        while True:
            message = await receive_message(websocket)
            now = time.monotonic()
            if now - window_start >= 1.0:
                window_start, count = now, 0
            count += 1
            if count > MAX_MESSAGES_PER_SECOND:
                await websocket.close(code=1008, reason="Too many messages")
                return
            if not message or message.get("type") != "direction":
                continue
            direction = DIRECTIONS.get(message.get("direction")) if isinstance(message.get("direction"), str) else None
            if direction:
                player = arena.players.get(player_id)
                if player:
                    player.next_direction = direction
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        logger.exception("WebSocket handler error")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if player_id:
            async with arena.lock:
                arena.remove(player_id)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8002, ws_max_size=WS_MAX_SIZE)
