import asyncio, os, json, time, hmac, hashlib, sqlite3, random, logging
from urllib.parse import parse_qsl
import chess
from aiohttp import web, WSMsgType
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton as B,
                           InlineKeyboardMarkup as KB, Message, WebAppInfo)

# ===== SOZLAMALAR =====
TOKEN = os.getenv("BOT_TOKEN", "BOT_TOKEN_SHU_YERGA")
WEBAPP = os.getenv("WEBAPP_URL", "https://SIZNING-DOMEN.uz")   # https manzil (index.html shu serverda ishlaydi)
ADMINS = [8160504896]
CARD = "8600 0000 0000 0000"          # depozit uchun admin kartasi
DEPOSITS = {12000: 2000, 24000: 4000, 60000: 10000}
MIN_WD = 3000                          # minimal yechish (tanga)
NEW_BONUS, REF_BONUS = 5, 20
STAKES = [50, 100, 500, 1000]
CLOCK = {"chess": 600, "checkers": 300, "connect4": 300, "snake": 60}

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN); dp = Dispatcher()

# ===== BAZA =====
con = sqlite3.connect("goldrex2.db", check_same_thread=False); con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, coins INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
 played INTEGER DEFAULT 0, xp INTEGER DEFAULT 0, ref_by INTEGER, ref_paid INTEGER DEFAULT 0, card TEXT);
CREATE TABLE IF NOT EXISTS wd(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, amount INTEGER, card TEXT, status TEXT DEFAULT 'new');
CREATE TABLE IF NOT EXISTS dep(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER, sum INTEGER, coins INTEGER, status TEXT DEFAULT 'new');
""")
def q(sql, a=(), one=False, commit=False):
    cur = con.execute(sql, a)
    if commit: con.commit(); return cur
    return cur.fetchone() if one else cur.fetchall()
def user(u): return q("SELECT * FROM users WHERE id=?", (u,), one=True)
def add(u, n): q("UPDATE users SET coins=coins+? WHERE id=?", (n, u), commit=True)
def lvl(xp): return min(100, 1 + xp // 50)
def nm(u): return user(u)["name"]
def reg(u, name, ref=None):
    if user(u): return False
    q("INSERT INTO users(id,name,coins,ref_by) VALUES(?,?,?,?)", (u, name, NEW_BONUS, ref), commit=True); return True

# ===== O'YIN SERVERI =====
online, queue, qd, ingame, games = {}, {}, {}, {}, []

async def send(u, m):
    w = online.get(u)
    if w:
        try: await w.send_json(m)
        except Exception: pass

async def sendme(u):
    x = user(u); await send(u, dict(t="me", name=x["name"], coins=x["coins"], wins=x["wins"], lvl=lvl(x["xp"]), online=len(online)))

def newapple(s):
    while True:
        a = (s["R"].randrange(16), s["R"].randrange(16))
        if a not in s["body"]: return a

class G:
    def __init__(s, kind, stake, uids):
        s.kind, s.stake, s.uids = kind, stake, uids
        s.turn, s.over, s.last, s.t = 0, False, time.time(), [CLOCK[kind]] * 2
        s.must, s.nocap = None, 0
        if kind == "chess": s.b = chess.Board()
        elif kind == "checkers":
            s.bd = [''] * 64
            for r in range(8):
                for c in range(8):
                    if (r + c) % 2: s.bd[r * 8 + c] = 'b' if r < 3 else ('w' if r > 4 else '')
        elif kind == "connect4": s.bd = [0] * 42
        else:
            s.t0 = time.time(); seed = random.randint(0, 10**9); s.sn = []
            for _ in range(2):
                x = dict(body=[(8, 5), (8, 4), (8, 3)], d=(0, 1), nd=(0, 1), score=0, alive=True, R=random.Random(seed))
                x["apple"] = newapple(x); s.sn.append(x)

def cmoves(bd, p, must):
    mine, en, fw = ("wW", "bB", -1) if p == 0 else ("bB", "wW", 1)
    caps, steps = [], []
    for i in ([must] if must is not None else range(64)):
        ch = bd[i]
        if not ch or ch not in mine: continue
        r, c = divmod(i, 8); king = ch.isupper()
        for dr, dc in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            r1, c1, r2, c2 = r + dr, c + dc, r + 2 * dr, c + 2 * dc
            if 0 <= r2 < 8 and 0 <= c2 < 8 and bd[r1 * 8 + c1] and bd[r1 * 8 + c1] in en and not bd[r2 * 8 + c2]:
                caps.append((i, r2 * 8 + c2, r1 * 8 + c1))
            if not king and dr != fw: continue
            if 0 <= r1 < 8 and 0 <= c1 < 8 and not bd[r1 * 8 + c1]: steps.append((i, r1 * 8 + c1, None))
    return caps or ([] if must is not None else steps)

def c4win(bd, r, c, p):
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        n = 1
        for s in (1, -1):
            rr, cc = r + dr * s, c + dc * s
            while 0 <= rr < 6 and 0 <= cc < 7 and bd[rr * 7 + cc] == p: n += 1; rr += dr * s; cc += dc * s
        if n >= 4: return True
    return False

def sq2i(sq): return (7 - chess.square_rank(sq)) * 8 + chess.square_file(sq)

def view(g, i):
    v = dict(t="state", kind=g.kind, you=i, turn=g.turn, names=[nm(u) for u in g.uids], stake=g.stake)
    if g.kind == "snake":
        me, op = g.sn[i], g.sn[1 - i]
        v.update(body=me["body"], apple=me["apple"], alive=me["alive"], sc=[me["score"], op["score"]],
                 left=max(0, CLOCK["snake"] - (time.time() - g.t0)))
    else:
        v["tl"] = g.t; legal = {}
        if g.kind == "chess":
            v["board"] = [(g.b.piece_at(chess.square(k % 8, 7 - k // 8)).symbol() if g.b.piece_at(chess.square(k % 8, 7 - k // 8)) else "") for k in range(64)]
            for m in g.b.legal_moves: legal.setdefault(sq2i(m.from_square), []).append(sq2i(m.to_square))
            v["check"] = g.b.is_check()
        elif g.kind == "checkers":
            v["board"] = g.bd
            for f, t_, _ in cmoves(g.bd, g.turn, g.must): legal.setdefault(f, []).append(t_)
        else: v["board"] = g.bd
        v["legal"] = legal
    return v

async def push(g):
    for i, u in enumerate(g.uids): await send(u, view(g, i))

async def finish(g, w):
    if g.over: return
    g.over = True; games.remove(g)
    for i, u in enumerate(g.uids):
        ingame.pop(u, None)
        if w is None: add(u, g.stake); res, xp, delta = "draw", 5, 0
        elif w == i: gain = int(g.stake * 2 * 0.8); add(u, gain); res, xp, delta = "win", 10, gain - g.stake
        else: res, xp, delta = "lose", 2, -g.stake
        first = user(u)["played"] == 0
        q("UPDATE users SET played=played+1, xp=xp+?, wins=wins+? WHERE id=?", (xp, int(res == "win"), u), commit=True)
        x = user(u)
        if first and x["ref_by"] and not x["ref_paid"]:      # referal: do'st 1-o'yinini o'ynadi
            add(x["ref_by"], REF_BONUS); q("UPDATE users SET ref_paid=1 WHERE id=?", (u,), commit=True)
            try: await bot.send_message(x["ref_by"], f"🎉 Taklif qilgan do'stingiz o'yin o'ynadi! +{REF_BONUS} tanga")
            except Exception: pass
        await send(u, dict(t="end", res=res, delta=delta)); await sendme(u)

async def tick(g, now):
    for s in g.sn:
        if not s["alive"]: continue
        s["d"] = s["nd"]; h = (s["body"][0][0] + s["d"][0], s["body"][0][1] + s["d"][1])
        if not (0 <= h[0] < 16 and 0 <= h[1] < 16) or h in s["body"][:-1]: s["alive"] = False; continue
        s["body"].insert(0, h)
        if h == s["apple"]: s["score"] += 1; s["apple"] = newapple(s)
        else: s["body"].pop()
    if now - g.t0 >= CLOCK["snake"] or not any(s["alive"] for s in g.sn):
        a, b = g.sn[0]["score"], g.sn[1]["score"]
        await finish(g, None if a == b else (0 if a > b else 1))
    else: await push(g)

async def loop():
    while True:
        await asyncio.sleep(0.2); now = time.time()
        for g in list(games):
            if g.over: continue
            if g.kind == "snake": await tick(g, now)
            elif g.t[g.turn] - (now - g.last) <= 0: await finish(g, 1 - g.turn)

async def do_move(g, i, d):
    if g.over: return
    if g.kind == "snake":
        nd = tuple(d.get("d", ())); s = g.sn[i]
        if nd in ((0, 1), (0, -1), (1, 0), (-1, 0)) and (nd[0] + s["d"][0], nd[1] + s["d"][1]) != (0, 0): s["nd"] = nd
        return
    if g.turn != i: return
    now = time.time(); g.t[i] -= now - g.last; g.last = now
    if g.t[i] <= 0: return await finish(g, 1 - i)
    win, draw = None, False
    if g.kind == "chess":
        f, to = int(d["f"]), int(d["to"]); fs, ts = chess.square(f % 8, 7 - f // 8), chess.square(to % 8, 7 - to // 8)
        p = g.b.piece_at(fs)
        promo = chess.QUEEN if p and p.piece_type == chess.PAWN and chess.square_rank(ts) in (0, 7) else None
        mv = chess.Move(fs, ts, promo)
        if mv not in g.b.legal_moves: return
        g.b.push(mv); g.turn = 0 if g.b.turn else 1
        if g.b.is_checkmate(): win = i
        elif g.b.is_game_over(): draw = True
    elif g.kind == "checkers":
        f, to = int(d["f"]), int(d["to"])
        mv = [x for x in cmoves(g.bd, i, g.must) if x[0] == f and x[1] == to]
        if not mv: return
        cp = mv[0][2]; ch = g.bd[f]; g.bd[f] = ''; g.bd[to] = ch; g.nocap = 0 if cp is not None else g.nocap + 1
        if cp is not None: g.bd[cp] = ''
        promoted = ch.islower() and to // 8 == (0 if i == 0 else 7)
        if promoted: g.bd[to] = ch.upper()
        if cp is not None and not promoted and any(x[2] is not None for x in cmoves(g.bd, i, to)): g.must = to
        else:
            g.must = None; g.turn = 1 - i
            if not cmoves(g.bd, g.turn, None): win = i
            elif g.nocap >= 60: draw = True
    else:
        c = int(d["col"])
        if not 0 <= c < 7: return
        for r in range(5, -1, -1):
            if not g.bd[r * 7 + c]: break
        else: return
        g.bd[r * 7 + c] = i + 1
        if c4win(g.bd, r, c, i + 1): win = i
        elif all(g.bd): draw = True
        else: g.turn = 1 - i
    if win is not None or draw: await push(g); await finish(g, win)
    else: await push(g)

def auth(init):
    d = dict(parse_qsl(init)); h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not h or not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h): return None
    return json.loads(d["user"])

async def ws_h(req):
    ws = web.WebSocketResponse(heartbeat=20); await ws.prepare(req); uid = None
    try:
        async for m in ws:
            if m.type != WSMsgType.TEXT: continue
            d = json.loads(m.data)
            if uid is None:
                u = auth(d.get("init", ""))
                if not u: break
                uid = u["id"]; reg(uid, u.get("first_name", "Player")); online[uid] = ws; await sendme(uid)
                if uid in ingame: g = ingame[uid]; await send(uid, view(g, g.uids.index(uid)))
                continue
            t, g = d.get("t"), ingame.get(uid)
            if t == "join" and not g and uid not in qd:
                kind, stake = d.get("game"), int(d.get("stake", 0))
                if kind not in CLOCK or stake not in STAKES: continue
                if user(uid)["coins"] < stake: await send(uid, dict(t="err", msg="Tanga yetarli emas")); continue
                add(uid, -stake); key = (kind, stake); opp = queue.pop(key, None)
                if opp and opp != uid:
                    qd.pop(opp, None); g = G(kind, stake, [opp, uid]); games.append(g)
                    ingame[opp] = ingame[uid] = g; await push(g); await sendme(uid); await sendme(opp)
                else: queue[key] = uid; qd[uid] = key; await send(uid, dict(t="queue")); await sendme(uid)
            elif t == "cancel" and uid in qd:
                key = qd.pop(uid); queue.pop(key, None); add(uid, key[1]); await sendme(uid)
            elif t == "move" and g: await do_move(g, g.uids.index(uid), d)
            elif t == "resign" and g: await finish(g, 1 - g.uids.index(uid))
    finally:
        if uid and online.get(uid) is ws:
            del online[uid]
            if uid in qd: key = qd.pop(uid); queue.pop(key, None); add(uid, key[1])
    return ws

async def page(_): return web.FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))

# ===== BOT =====
def home_kb():
    return KB(inline_keyboard=[
        [B(text="▶️ O'YNASH", web_app=WebAppInfo(url=WEBAPP))],
        [B(text="💰 Depozit", callback_data="dep"), B(text="💸 Pul chiqarish", callback_data="wd")],
        [B(text="💳 Karta qo'shish", callback_data="card"), B(text="👥 Referal", callback_data="ref")],
        [B(text="🏆 Reyting", callback_data="rt:wins"), B(text="👤 Profil", callback_data="home")]])
def back(): return KB(inline_keyboard=[[B(text="⬅️ Orqaga", callback_data="home")]])
def ptext(u):
    card = f"•••• {u['card'][-4:]}" if u["card"] else "qo'shilmagan"
    return (f"👑 <b>GOLDREX</b>\n\n👤 {u['name']}\n🆔 <code>{u['id']}</code>\n🛡 Daraja: {lvl(u['xp'])} LVL\n"
            f"🪙 Tanga: {u['coins']:,}\n🏅 G'alaba: {u['wins']} | 🎮 O'yin: {u['played']}\n💳 Karta: {card}")
async def show(c, text, kb=None):
    try: await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception: await c.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.message(CommandStart())
async def start(m: Message):
    uid = m.from_user.id; p = (m.text or "").split()
    ref = int(p[1][4:]) if len(p) > 1 and p[1].startswith("ref_") and p[1][4:].isdigit() else None
    if ref and (ref == uid or not user(ref)): ref = None
    if reg(uid, m.from_user.full_name, ref): await m.answer(f"🎁 Xush kelibsiz! Bonus: +{NEW_BONUS} tanga")
    await m.answer(ptext(user(uid)), reply_markup=home_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear(); await show(c, ptext(user(c.from_user.id)), home_kb())

@dp.callback_query(F.data == "ref")
async def ref(c: CallbackQuery):
    me = (await bot.get_me()).username
    await show(c, f"👥 <b>Referal</b>\nDo'stingiz sizning havolangiz orqali kirib, <b>1 ta o'yin o'ynasa</b> sizga +{REF_BONUS} tanga beriladi.\n"
                  f"Har yangi foydalanuvchi +{NEW_BONUS} tanga bonus oladi.\n\n🔗 https://t.me/{me}?start=ref_{c.from_user.id}", back())

# --- Karta ---
class Card(StatesGroup): num = State()
@dp.callback_query(F.data == "card")
async def card(c: CallbackQuery, state: FSMContext):
    await state.set_state(Card.num); await show(c, "💳 Pul chiqarish uchun karta raqamingizni (16 raqam) yuboring:", back())
@dp.message(Card.num)
async def card_save(m: Message, state: FSMContext):
    n = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if len(n) != 16: return await m.answer("16 ta raqam yuboring.")
    q("UPDATE users SET card=? WHERE id=?", (n, m.from_user.id), commit=True); await state.clear()
    await m.answer(f"✅ Karta saqlandi: •••• {n[-4:]}", reply_markup=back())

# --- Depozit ---
class Dep(StatesGroup): chk = State()
@dp.callback_query(F.data == "dep")
async def dep(c: CallbackQuery):
    rows = [[B(text=f"{s:,} so'm → {t:,} tanga", callback_data=f"pay:{s}")] for s, t in DEPOSITS.items()]
    await show(c, "💰 <b>Depozit</b>\nPaketni tanlang:", KB(inline_keyboard=rows + back().inline_keyboard))
@dp.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery, state: FSMContext):
    s = int(c.data[4:]); await state.set_state(Dep.chk); await state.update_data(s=s)
    await show(c, f"💳 Kartaga <b>{s:,} so'm</b> o'tkazing:\n<code>{CARD}</code>\n\nTo'lov chekining <b>rasmini</b> yuboring.", back())
@dp.message(Dep.chk, F.photo)
async def dep_chk(m: Message, state: FSMContext):
    s = (await state.get_data())["s"]; await state.clear()
    i = q("INSERT INTO dep(uid,sum,coins) VALUES(?,?,?)", (m.from_user.id, s, DEPOSITS[s]), commit=True).lastrowid
    kb = KB(inline_keyboard=[[B(text="✅ Tasdiqlash", callback_data=f"dok:{i}"), B(text="❌ Rad etish", callback_data=f"dno:{i}")]])
    for a in ADMINS:
        await bot.send_photo(a, m.photo[-1].file_id, reply_markup=kb,
            caption=f"💰 Depozit #{i}\n{m.from_user.full_name} ({m.from_user.id})\n{s:,} so'm → {DEPOSITS[s]:,} tanga")
    await m.answer("Chek yuborildi. Admin tasdiqlagach tanga qo'shiladi ✅", reply_markup=back())
@dp.callback_query(F.data.regexp(r"^d(ok|no):"))
async def dep_admin(c: CallbackQuery):
    if c.from_user.id not in ADMINS: return await c.answer()
    act, i = c.data.split(":"); r = q("SELECT * FROM dep WHERE id=?", (i,), one=True)
    if not r or r["status"] != "new": return await c.answer("Ko'rib chiqilgan")
    ok = act == "dok"; q("UPDATE dep SET status=? WHERE id=?", ("ok" if ok else "no", i), commit=True)
    if ok: add(r["uid"], r["coins"])
    await bot.send_message(r["uid"], f"✅ Depozit tasdiqlandi: +{r['coins']:,} tanga" if ok else "❌ Depozit rad etildi")
    await c.message.edit_caption(caption=(c.message.caption or "") + ("\n\n✅ Tasdiqlandi" if ok else "\n\n❌ Rad etildi"))

# --- Pul chiqarish ---
class Wd(StatesGroup): amt = State()
@dp.callback_query(F.data == "wd")
async def wd(c: CallbackQuery, state: FSMContext):
    u = user(c.from_user.id)
    if not u["card"]: return await c.answer("Avval karta qo'shing (💳 Karta qo'shish)", show_alert=True)
    await state.set_state(Wd.amt)
    await show(c, f"💸 <b>Pul chiqarish</b>\nMinimal: {MIN_WD:,} tanga (15 000 so'm va undan ko'p)\nBalans: {u['coins']:,} tanga\n"
                  f"Karta: •••• {u['card'][-4:]}\n\nMiqdorni yozing:", back())
@dp.message(Wd.amt)
async def wd_amt(m: Message, state: FSMContext):
    u = user(m.from_user.id)
    if not (m.text or "").isdigit(): return await m.answer("Faqat raqam yozing.")
    n = int(m.text)
    if n < MIN_WD or n > u["coins"]: return await m.answer(f"Miqdor {MIN_WD:,} dan kam yoki balansdan ko'p bo'lmasin.")
    await state.clear(); add(u["id"], -n)
    i = q("INSERT INTO wd(uid,amount,card) VALUES(?,?,?)", (u["id"], n, u["card"]), commit=True).lastrowid
    kb = KB(inline_keyboard=[[B(text="✅ To'landi", callback_data=f"wok:{i}"), B(text="❌ Rad etish", callback_data=f"wno:{i}")]])
    for a in ADMINS:
        await bot.send_message(a, f"💸 So'rov #{i}\n{u['name']} (<code>{u['id']}</code>)\n{n:,} tanga\nKarta: <code>{u['card']}</code>", reply_markup=kb, parse_mode="HTML")
    await m.answer("So'rov adminga yuborildi ✅", reply_markup=back())
@dp.callback_query(F.data.regexp(r"^w(ok|no):"))
async def wd_admin(c: CallbackQuery):
    if c.from_user.id not in ADMINS: return await c.answer()
    act, i = c.data.split(":"); r = q("SELECT * FROM wd WHERE id=?", (i,), one=True)
    if not r or r["status"] != "new": return await c.answer("Ko'rib chiqilgan")
    ok = act == "wok"; q("UPDATE wd SET status=? WHERE id=?", ("ok" if ok else "no", i), commit=True)
    if not ok: add(r["uid"], r["amount"])
    await bot.send_message(r["uid"], "✅ To'lov kartangizga yuborildi" if ok else "❌ So'rov rad etildi, tanga qaytarildi")
    await c.message.edit_text(c.message.text + ("\n\n✅ To'landi" if ok else "\n\n❌ Rad etildi"))

# --- Reyting ---
@dp.callback_query(F.data.startswith("rt:"))
async def rating(c: CallbackQuery):
    k = c.data[3:]; col = {"wins": "wins", "coins": "coins", "lvl": "xp"}[k]
    top = q(f"SELECT name,coins,wins,xp FROM users ORDER BY {col} DESC LIMIT 10")
    val = lambda r: {"wins": r["wins"], "coins": f"{r['coins']:,}", "lvl": f"{lvl(r['xp'])} LVL"}[k]
    mk = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    txt = "\n".join(f"{mk[i]} {r['name']} — {val(r)}" for i, r in enumerate(top)) or "Hali yo'q"
    tabs = [B(text="🏅 G'alaba", callback_data="rt:wins"), B(text="🪙 Tanga", callback_data="rt:coins"), B(text="🛡 Daraja", callback_data="rt:lvl")]
    await show(c, f"🏆 <b>Reyting</b>\n\n{txt}", KB(inline_keyboard=[tabs] + back().inline_keyboard))

# --- Admin ---
class Adm(StatesGroup): bc = State(); zero = State()
def is_admin(m): return m.from_user.id in ADMINS
@dp.message(Command("admin"), is_admin)
async def admin(m: Message):
    kb = KB(inline_keyboard=[[B(text="👥 Foydalanuvchilar", callback_data="a:users"), B(text="📢 Xabar yuborish", callback_data="a:bc")],
                             [B(text="🚫 Balansni 0 qilish", callback_data="a:zero"), B(text="🏆 Reytingni tozalash", callback_data="a:wipe")]])
    await m.answer("👑 <b>Admin panel</b>", reply_markup=kb, parse_mode="HTML")
@dp.callback_query(F.data.startswith("a:"))
async def admin_cb(c: CallbackQuery, state: FSMContext):
    if c.from_user.id not in ADMINS: return await c.answer()
    a = c.data[2:]
    if a == "users": await c.message.answer(f"👥 Jami: {q('SELECT COUNT(*) n FROM users', one=True)['n']} | Online: {len(online)}")
    elif a == "bc": await state.set_state(Adm.bc); await c.message.answer("Xabarni yozing:")
    elif a == "zero": await state.set_state(Adm.zero); await c.message.answer("Foydalanuvchi ID sini yozing:")
    elif a == "wipe": q("UPDATE users SET wins=0, xp=0", commit=True); await c.message.answer("Reyting tozalandi ✅")
    await c.answer()
@dp.message(Adm.bc, is_admin)
async def do_bc(m: Message, state: FSMContext):
    await state.clear(); ok = 0
    for r in q("SELECT id FROM users"):
        try: await bot.copy_message(r["id"], m.chat.id, m.message_id); ok += 1; await asyncio.sleep(0.05)
        except Exception: pass
    await m.answer(f"Yuborildi: {ok}")
@dp.message(Adm.zero, is_admin)
async def do_zero(m: Message, state: FSMContext):
    await state.clear()
    if (m.text or "").isdigit(): q("UPDATE users SET coins=0 WHERE id=?", (int(m.text),), commit=True); await m.answer("Balans 0 ✅")

async def main():
    app = web.Application(); app.add_routes([web.get("/", page), web.get("/ws", ws_h)])
    r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
    asyncio.create_task(loop()); await dp.start_polling(bot)
if __name__ == "__main__": asyncio.run(main())
