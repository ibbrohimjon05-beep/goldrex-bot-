
import os
import time
import json
import hmac
import hashlib
import sqlite3
import logging
import threading
import random
from urllib.parse import parse_qsl

import chess
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, join_room, emit
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "10000"))
DB = os.getenv("DB_PATH", "goldrex.db")
SECRET_KEY = os.getenv("SECRET_KEY", "")

logging.basicConfig(level=logging.INFO)
app = Flask(__name__, template_folder="templates")
app.config["SECRET_KEY"] = SECRET_KEY or os.urandom(32).hex()
socketio = SocketIO(app, cors_allowed_origins=[], async_mode="threading")

LOCK = threading.RLock()
rooms = {}
waiting = {}
online = set()

BET_OPTIONS = (3, 10, 50, 100, 500, 1000)


def db():
    c = sqlite3.connect(DB, timeout=20)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            coins INTEGER NOT NULL DEFAULT 5,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            game TEXT,
            result TEXT,
            created REAL
        )
        """)


def get_user(uid, name="Player"):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(user_id,username,coins) VALUES(?,?,5)",
            (uid, name[:32])
        )
        c.execute("UPDATE users SET username=? WHERE user_id=?",
                  (name[:32], uid))
        row = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        return dict(row)


def verify_init_data(init_data):
    if not BOT_TOKEN or not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None

        check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(pairs.items())
        )
        secret = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret, check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received):
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        if abs(time.time() - auth_date) > 86400:
            return None

        user = json.loads(pairs.get("user", "{}"))
        return user if user.get("id") else None
    except Exception:
        return None


def user_from_request():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    user = verify_init_data(init_data)
    if not user:
        return None
    return user


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify(status="GOLDREX online")


@app.route("/api/me", methods=["POST"])
def api_me():
    user = user_from_request()
    if not user:
        return jsonify(error="Telegram tasdiqlanmadi"), 401
    u = get_user(user["id"], user.get("first_name", "Player"))
    return jsonify(u)


@app.route("/api/leaderboard")
def leaderboard():
    with db() as c:
        rows = c.execute(
            "SELECT username,wins,coins,level FROM users "
            "ORDER BY wins DESC,coins DESC LIMIT 20"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/online")
def api_online():
    return jsonify(count=len(online))


def new_room(game, bet):
    return {
        "id": str(random.randint(100000, 999999)),
        "game": game,
        "bet": bet,
        "players": [],
        "state": {},
        "started": False,
        "created": time.time(),
        "started_at": None,
        "turn_started_at": None
    }


def room_for_sid(sid):
    for room in rooms.values():
        for p in room["players"]:
            if p["sid"] == sid:
                return room, p
    return None, None


def emit_state(room):
    socketio.emit("state", {"state": room["state"]}, to=room["id"])


def make_checkers_board():
    board = [[0] * 8 for _ in range(8)]
    for r in range(3):
        for c in range(8):
            if (r + c) % 2:
                board[r][c] = 2
    for r in range(5, 8):
        for c in range(8):
            if (r + c) % 2:
                board[r][c] = 1
    return board


def checkers_moves(board, side, r, c):
    piece = board[r][c]
    if not piece or (1 if piece in (1, 3) else 2) != side:
        return []
    king = piece in (3, 4)
    dirs = [(-1, -1), (-1, 1)] if side == 1 else [(1, -1), (1, 1)]
    if king:
        dirs = [(-1,-1),(-1,1),(1,-1),(1,1)]
    moves = []
    for dr, dc in dirs:
        nr, nc = r + dr, c + dc
        if 0 <= nr < 8 and 0 <= nc < 8:
            if board[nr][nc] == 0:
                moves.append((nr, nc, None))
            else:
                er, ec = nr + dr, nc + dc
                enemy = board[nr][nc]
                enemy_side = 1 if enemy in (1, 3) else 2
                if enemy_side != side and 0 <= er < 8 and 0 <= ec < 8 and board[er][ec] == 0:
                    moves.append((er, ec, (nr, nc)))
    return moves


def all_checkers_moves(board, side):
    result = []
    captures = []
    for r in range(8):
        for c in range(8):
            piece = board[r][c]
            if piece and (1 if piece in (1, 3) else 2) == side:
                for nr, nc, cap in checkers_moves(board, side, r, c):
                    item = (r, c, nr, nc, cap)
                    (captures if cap else result).append(item)
    return captures or result


def start_state(game):
    if game == "connect4":
        return {"board": [[0]*7 for _ in range(6)], "turn": 0}
    if game == "checkers":
        return {"board": make_checkers_board(), "turn": 0, "selected": None}
    if game == "chess":
        return {"fen": chess.STARTING_FEN, "turn": 0}
    if game == "snake":
        return {
            "players": [
                {"x": 4, "y": 4, "dir": "R", "score": 0},
                {"x": 15, "y": 15, "dir": "L", "score": 0}
            ],
            "food": {"x": 10, "y": 10},
            "turn": 0
        }


def connect_winner(board, side):
    for r in range(6):
        for c in range(7):
            if board[r][c] != side:
                continue
            for dr, dc in ((0,1),(1,0),(1,1),(1,-1)):
                if all(
                    0 <= r+dr*k < 6 and 0 <= c+dc*k < 7
                    and board[r+dr*k][c+dc*k] == side
                    for k in range(4)
                ):
                    return True
    return False


def finish_room(room, winner, message):
    if not room.get("started"):
        return
    room["started"] = False
    bet = room["bet"]

    with db() as c:
        if winner is not None and bet:
            prize = bet * 2 * 80 // 100
            for i, p in enumerate(room["players"]):
                if i == winner:
                    c.execute(
                        "UPDATE users SET coins=coins+?,wins=wins+1,"
                        "level=1+(wins+1)/10 WHERE user_id=?",
                        (prize, p["uid"])
                    )
                else:
                    c.execute(
                        "UPDATE users SET losses=losses+1 WHERE user_id=?",
                        (p["uid"],)
                    )
        elif winner is not None:
            for i, p in enumerate(room["players"]):
                c.execute(
                    "UPDATE users SET wins=wins+? ,losses=losses+? WHERE user_id=?",
                    (1 if i == winner else 0, 0 if i == winner else 1, p["uid"])
                )

        for p in room["players"]:
            c.execute(
                "INSERT INTO results(user_id,game,result,created) VALUES(?,?,?,?)",
                (p["uid"], room["game"],
                 "win" if winner is not None and room["players"].index(p)==winner else "end",
                 time.time())
            )

    for i, p in enumerate(room["players"]):
        socketio.emit("game_over", {
            "winner": winner,
            "you": i,
            "text": message if winner is None else (
                "Siz yutdingiz!" if i == winner else "Siz yutqazdingiz!"
            )
        }, to=p["sid"])
    rooms.pop(room["id"], None)


@socketio.on("connect")
def connected(auth):
    auth = auth or {}
    user = verify_init_data(auth.get("initData", ""))
    if not user:
        return False
    uid = int(user["id"])
    name = user.get("first_name", "Player")[:32]
    get_user(uid, name)
    request.environ["goldrex_uid"] = uid
    request.environ["goldrex_name"] = name
    online.add(uid)
    emit("online", {"count": len(online)})


@socketio.on("disconnect")
def disconnected():
    uid = request.environ.get("goldrex_uid")
    if uid:
        online.discard(uid)
    sid = request.sid
    for game, item in list(waiting.items()):
        if item["sid"] == sid:
            waiting.pop(game, None)
    room, player = room_for_sid(sid)
    if room:
        finish_room(room, 1 - room["players"].index(player), "Raqib uzildi")


@socketio.on("join_queue")
def join_queue(data):
    uid = request.environ.get("goldrex_uid")
    name = request.environ.get("goldrex_name", "Player")
    if not uid:
        emit("notice", {"text": "Telegram tasdiqlanmadi"})
        return

    game = data.get("game")
    bet = int(data.get("bet", 0))
    if game not in ("chess", "checkers", "connect4", "snake"):
        emit("notice", {"text": "Noma’lum o‘yin"})
        return
    if bet not in (0,) + BET_OPTIONS:
        emit("notice", {"text": "Noto‘g‘ri tanga miqdori"})
        return

    with LOCK:
        if waiting.get(game) and waiting[game]["sid"] != request.sid:
            other = waiting.pop(game)
            if other["bet"] != bet:
                waiting[game] = other
                emit("queued", {"text": "Mos stavkali raqib qidirilmoqda"})
                return

            room = new_room(game, bet)
            room["players"] = [
                other,
                {"sid": request.sid, "uid": uid, "name": name}
            ]

            # Virtual coins only; no purchase or cashout.
            with db() as c:
                for p in room["players"]:
                    row = c.execute(
                        "SELECT coins FROM users WHERE user_id=?", (p["uid"],)
                    ).fetchone()
                    if not row or row["coins"] < bet:
                        emit("notice", {"text": "Tanga yetarli emas"})
                        waiting[game] = other
                        return
                for p in room["players"]:
                    c.execute("UPDATE users SET coins=coins-? WHERE user_id=?",
                              (bet, p["uid"]))

            room["state"] = start_state(game)
            room["started"] = True
            room["started_at"] = time.time()
            room["turn_started_at"] = time.time()
            rooms[room["id"]] = room

            join_room(room["id"])
            socketio.server.enter_room(other["sid"], room["id"], namespace="/")
            for i, p in enumerate(room["players"]):
                socketio.emit("match_found", {
                    "room": room["id"], "game": game, "you": i,
                    "opponent": room["players"][1-i]["name"],
                    "state": room["state"], "bet": bet
                }, to=p["sid"])
        else:
            waiting[game] = {
                "sid": request.sid, "uid": uid, "name": name, "bet": bet
            }
            emit("queued", {"text": "Raqib qidirilmoqda..."})


@socketio.on("game_action")
def game_action(data):
    room, player = room_for_sid(request.sid)
    if not room or not room["started"]:
        return
    idx = room["players"].index(player)
    game, state = room["game"], room["state"]
    action = data.get("action", {})

    if game in ("connect4", "checkers", "chess") and state["turn"] != idx:
        return

    if game == "connect4":
        try:
            col = int(action.get("col", -1))
        except (TypeError, ValueError):
            return
        if not 0 <= col < 7:
            return
        row = next((r for r in range(5,-1,-1) if state["board"][r][col] == 0), None)
        if row is None:
            return
        side = idx + 1
        state["board"][row][col] = side
        won = connect_winner(state["board"], side)
        draw = all(state["board"][0])
        state["turn"] = 1 - idx
        emit_state(room)
        if won:
            finish_room(room, idx, "G‘alaba!")
        elif draw:
            finish_room(room, None, "Durang!")

    elif game == "checkers":
        try:
            r, c = int(action.get("r", -1)), int(action.get("c", -1))
        except (TypeError, ValueError):
            return
        if not (0 <= r < 8 and 0 <= c < 8):
            return
        board = state["board"]
        side = idx + 1
        selected = state.get("selected")
        if selected is None:
            if board[r][c] and (1 if board[r][c] in (1,3) else 2) == side:
                state["selected"] = [r,c]
                emit_state(room)
            return

        sr, sc = selected
        legal = all_checkers_moves(board, side)
        move = next((m for m in legal if m[:2] == (sr,sc) and m[2:4] == (r,c)), None)
        if not move:
            if board[r][c] and (1 if board[r][c] in (1,3) else 2) == side:
                state["selected"] = [r,c]
            else:
                state["selected"] = None
            emit_state(room)
            return

        piece = board[sr][sc]
        board[r][c] = piece
        board[sr][sc] = 0
        cap = move[4]
        if cap:
            board[cap[0]][cap[1]] = 0
        if side == 1 and r == 0 and piece == 1:
            board[r][c] = 3
        if side == 2 and r == 7 and piece == 2:
            board[r][c] = 4

        state["selected"] = None
        state["turn"] = 1 - idx
        room["turn_started_at"] = time.time()
        if not all_checkers_moves(board, 1-side):
            finish_room(room, idx, "G‘alaba!")
        else:
            emit_state(room)

    elif game == "chess":
        try:
            board = chess.Board(state["fen"])
            move = chess.Move.from_uci(str(action.get("move", "")))
            if move not in board.legal_moves:
                return
            board.push(move)
        except Exception:
            return

        state["fen"] = board.fen()
        state["turn"] = 1 - idx
        room["turn_started_at"] = time.time()
        emit_state(room)

        if board.is_checkmate():
            finish_room(room, idx, "Shaxmat mat!")
        elif board.is_game_over():
            finish_room(room, None, "Durang!")

    elif game == "snake":
        direction = action.get("dir")
        if direction not in ("U","D","L","R"):
            return
        p = state["players"][idx]
        if (p["dir"], direction) not in (("U","D"),("D","U"),("L","R"),("R","L")):
            p["dir"] = direction
            emit_state(room)


def watchdog():
    while True:
        time.sleep(1)
        now = time.time()
        with LOCK:
            for room in list(rooms.values()):
                if not room.get("started"):
                    continue
                game = room["game"]
                if game == "chess":
                    turn = room["state"]["turn"]
                    if now - room["started_at"] >= 600:
                        finish_room(room, 1-turn, "10 daqiqalik vaqt tugadi")
                    elif now - room["turn_started_at"] >= 60:
                        finish_room(room, 1-turn, "Yurish uchun 1 daqiqa tugadi")


threading.Thread(target=watchdog, daemon=True).start()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user.id, user.first_name or "Player")
    buttons = []
    if WEBAPP_URL.startswith("https://"):
        buttons.append([InlineKeyboardButton(
            "🎮 GOLDREX o‘yinlari",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )])
    if user.id == ADMIN_ID:
        buttons.append([InlineKeyboardButton("👑 Admin panel", callback_data="admin")])
    await update.message.reply_text(
        "👑 GOLDREX\nO‘yin platformasiga xush kelibsiz!",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    await update.message.reply_text(
        f"Foydalanuvchilar: {users}\nOnline: {len(online)}\n"
        f"Kutayotgan o‘yinlar: {len(waiting)}"
    )


def run_bot():
    if not BOT_TOKEN:
        return
    bot = Application.builder().token(BOT_TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("stats", stats))
    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    init_db()
    if BOT_TOKEN:
        threading.Thread(target=run_bot, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=PORT, allow_unsafe_werkzeug=True)
