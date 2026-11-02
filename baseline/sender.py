#!/usr/bin/env python3
"""Xiaomei QQ sender - phase 6, stage 2: SEND APPROVED DRAFTS.

Authorization history (user, 2026-10-24):
- 21:59 group sending opened for the prayer-room group only.
- 22:04 both private chats opened too. ROUTES below is the full send scope;
  the sender sends NOTHING anywhere else.
- 22:07 user asked for stronger "活人感": multi-part messages, QQ faces,
  and proactive group posts (heartbeat drafts flagged "proactive").

Design contract:
- Consumes the bridge's drafts.jsonl (stage 1 output). Only records whose
  key is in ROUTES and decision == REPLY are candidates.
- Master gate: the SEND_ENABLED file in this directory must exist. It is
  re-checked before EVERY outbound POST (not only per draft), so closing the
  gate mid-reply stops the remaining parts.
- Kill switch (2026-10-27, audit G-2): if the bridge's KILLSWITCH
  file exists, post() refuses every segment before any POST
  (ledger reason GATE_KILLSWITCH). The mute is therefore an exit
  gate at the single outbound chokepoint, not only an entry gate
  at the bridge.
- Reliability (2026-10-27, audit B-02): each candidate line gets a stable
  draft id (byte offset + sha1 of the raw line). Before any work the id is
  written to inflight.json with fsync; only after a terminal outcome
  (SENT / FAILED / filtered) is it removed and the tail offset advanced.
  On startup leftovers are reported as INFLIGHT_LOST (stderr +
  ../sends-alerts.jsonl) - observable loss, never auto-resent.
- Brake (audit B-03): the 100/hour budget counts actual outbound SEGMENTS.
  Every segment (anthem voice+sticker+slogan, song, text, voice, typo fix)
  goes through one check: gate -> brake -> POST -> ledger append.
- Expression: draft_text may contain \n -> sent as up to 3 separate short
  messages with human-like gaps. A line prefixed [voice] is spoken via
  the ElevenLabs Xiaoxi clone (bridge/tts_eleven.py), falling
  back to MiMo VoiceDesign (bridge/tts_mimo.py), and sent as a
  record segment;
  any voice failure falls back to texting that line (audit: kind=voice).
  The model may use [face:ID] tokens with
  IDs from FACE_ALLOW; the sender renders them as QQ face segments. Raw
  CQ codes in model output are rejected (injection guard).
- Safety filters: <=80 visible chars per part, no "麻麻" (QQ-scene hard
  rule), no @全体成员, no URLs, no prompt/meta talk.
- NO pacing limits on replies (user's call, 2026-10-24 22:12: anti-spam
  pacing was a Bilibili-danmaku rule and does not apply to QQ; the
  model's own REPLY/NO_REPLY judgment is the flow control). Only a
  runaway circuit breaker remains: 100 segments/hour per conversation.
  Proactive drafts keep a budget: group only, >=1h apart, <=6/day.
- Every outcome is appended to ../sends.jsonl (SENT / HELD / RATE_LIMITED /
  PROACTIVE_LIMITED / GATE_OFF / FAILED) with incoming text + draft for
  audit and the Notion 弹幕库 backfill.
- Observability (2026-10-27, batch 2): sends records carry the draft's
  transaction id ("tx", passed through from events via drafts) and, for
  SENT/partial outcomes, "message_ids" with EVERY segment's message_id -
  quote resolution indexes all of them, so quoting any single part (e.g.
  a song's voice segment) resolves exactly instead of falling through.
- Tailing offset persists in state.json across restarts: drafts written
  while the sender was down are never sent retroactively.
"""
import hashlib, json, os, random, re, subprocess, sys, time, urllib.request
from pathlib import Path

QQ_DIR = Path(__file__).resolve().parent.parent
SENDER_DIR = Path(__file__).resolve().parent
DRAFTS_LOG = QQ_DIR / "drafts.jsonl"
# Batch-primary duplicate claims (P1C-IMP-01, 2026-11-01): the bridge
# stamps every draft with batch_primary = sha256(key|batch_id). The
# first handler to claim the file sends; any second candidate from the
# same batch (re-draft, replay, double handoff) is DUP_BLOCKED. The
# claim is content-independent - draft text plays no part in it.
DUP_CLAIMS_DIR = QQ_DIR / "sender" / "dup-claims"
SENDS_LOG = QQ_DIR / "sends.jsonl"
ALERT_LOG = QQ_DIR / "sends-alerts.jsonl"  # 与 sends.jsonl 同目录的告警账
UNKNOWN_LOG = QQ_DIR / "sends-unknown.jsonl"  # R-03：POST 结果未知的单独账
STATE_FILE = SENDER_DIR / "state.json"
INFLIGHT_FILE = SENDER_DIR / "inflight.json"
ENABLE_FILE = SENDER_DIR / "SEND_ENABLED"
KILLSWITCH_FILE = QQ_DIR / "bridge" / "KILLSWITCH"
RUBBER_FILE = SENDER_DIR / "RUBBER_ENABLED"
try:
    sys.path.insert(0, str(SENDER_DIR))
    from rubber import apply_rubber
except Exception:
    apply_rubber = None
TOKEN = (QQ_DIR / "onebot-token.txt").read_text().strip()
API_BASE = "http://127.0.0.1:3000"
ROUTES = {
    "grp-81658512": ("group", 81658512),        # 秋秋祈祷室～直播家人群
    "dm-1480760876": ("private", 1480760876),   # 草莓奶昔（用户大号）
    "dm-1689332102": ("private", 1689332102),   # 仲宥
}
BRAKE_PER_HOUR = 100  # runaway circuit breaker only - NOT pacing; counts SEGMENTS
PROACTIVE_MIN_INTERVAL = 3600
PROACTIVE_MAX_PER_DAY = 6
MAX_LEN = 80
MAX_PARTS = 3
# 31 blacklisted 2026-10-25 (user order): in current QQ clients id 31
# renders as an angry roaring face; the old table mislabeled it
# as Yiwen. 18 (Zumai/cursing) has never been allowed.
FACE_ALLOW = {4, 5, 6, 9, 12, 13, 14, 20, 21, 28, 32}
TYPO_PAIRS = [("的", "得"), ("得", "的"), ("地", "的"), ("在", "再"),
              ("再", "在"), ("做", "作"), ("以", "已"), ("已", "以"),
              ("像", "象"), ("园", "圆")]
TYPO_PROB = 0.0  # disabled 2026-10-26 by user order

def plan_typo(parts, skip=()):
    """MaiBot-style chinese_typo: rarely plan a one-char typo in the
    longest plain-text part; the caller swaps it in, sends, and then
    posts a quoted '*correct-char' correction like a human would.
    Indices in `skip` (voice parts) are never typo'd - speech has no
    typos, and the spoken text must stay exactly as drafted."""
    if random.random() >= TYPO_PROB:
        return None
    best = None
    for i, p in enumerate(parts):
        if i in skip or "[CQ:" in p:
            continue
        if len(p) >= 8 and (best is None or len(p) > len(parts[best])):
            best = i
    if best is None:
        return None
    for right, wrong in TYPO_PAIRS:
        if right in parts[best]:
            return (best, wrong, right)
    return None

STICKERS_DIR = QQ_DIR / "stickers"
STICKER_RE = re.compile(r"\[sticker:([a-z0-9\-]+)\]")

# Voice (2026-10-25): a draft line prefixed with [voice] is spoken
# and sent as a record segment. Engine order (2026-10-28):
# ElevenLabs Xiaoxi clone first (bridge/tts_eleven.py); MiMo
# VoiceDesign (bridge/tts_mimo.py) is the automatic fallback.
# Writing "mimo" into voice-out/tts-engine disables ElevenLabs
# without a restart.
# Voice is garnish, never a failure point: TTS failure or a failed voice
# send falls back to sending the same line as plain text.
VOICE_RE = re.compile(r"^\[voice\]\s*")
TTS_MIMO = QQ_DIR / "bridge" / "tts_mimo.py"
TTS_ELEVEN = QQ_DIR / "bridge" / "tts_eleven.py"
TTS_ENGINE_FILE = QQ_DIR / "voice-out" / "tts-engine"
TTS_TIMEOUT = 90

def _run_tts(script, text):
    """Run one TTS engine script (same interpreter, subprocess-isolated);
    returns the wav Path it printed, or None."""
    try:
        p = subprocess.run([sys.executable, str(script), text],
                           capture_output=True, text=True, timeout=TTS_TIMEOUT)
        if p.returncode == 0:
            lines = (p.stdout or "").strip().splitlines()
            if lines:
                path = Path(lines[-1].strip())
                if path.exists() and path.stat().st_size > 1000:
                    return path
    except Exception:
        pass
    return None

def _eleven_enabled():
    try:
        return TTS_ENGINE_FILE.read_text(
            encoding="utf-8").strip().lower() != "mimo"
    except Exception:
        return True

def synth_voice(text):
    """Synthesize `text` to a wav: ElevenLabs Xiaoxi clone first
    (2026-10-28), MiMo VoiceDesign as automatic fallback.
    Returns a Path or None."""
    if _eleven_enabled():
        got = _run_tts(TTS_ELEVEN, text)
        if got is not None:
            return got
    return _run_tts(TTS_MIMO, text)

# Anthem gag (user rule, 2026-10-26): the bridge emits draft_text
# "[anthem]" when someone asks her to sing the national anthem. Expand
# it here into the fixed three-part bit: pre-made voice, the qiangguo
# sticker, then the slogan - in that order.
ANTHEM_WAV = QQ_DIR / "voice-out" / "dongnan-kuxingshan-v1.wav"
ANTHEM_STICKER_SLUG = "qiangguo"
ANTHEM_TEXT = "請黨放心，強國有我"

# Song requests (2026-10-26): the bridge emits draft_text "[song:<slug>]"
# when she is called and asked for a finished library song. Expand it
# here into the pre-made recording plus one short line, mirroring the
# anthem bit. Catalogue lives in voice-out/songs/index.json.
SONGS_DIR = QQ_DIR / "voice-out" / "songs"
SONG_RE = re.compile(r"^\[song:([a-z0-9_-]+)\]$")

def load_song_index():
    try:
        return (json.loads((SONGS_DIR / "index.json").read_text()).get("songs") or {})
    except Exception:
        return {}

def load_manifest():
    try:
        return (json.loads((STICKERS_DIR / "manifest.json").read_text()).get("stickers") or {})
    except Exception:
        return {}
BLOCK_SUBSTRINGS = ["麻麻", "全体成员", "http://", "https://",
                    "www.", "系统提示", "提示词", "语言模型", "prompt"]
FACE_RE = re.compile(r"\[face:(\d+)\]")

def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _preserve_corrupt_state(err):
    """B-02 残余加固（GPT 复审 2026-10-27）：state.json 读取/解析失败时，
    先把原文件保全改名＋落 ALERT，再回退。旧实现静默吞掉异常直接按
    草稿文件末尾回退 offset——未消费草稿会被跳过且不留任何痕迹；保全
    文件让事后对账能把当时的 offset 找回来。回退行为本身不变。"""
    saved = None
    try:
        dst = STATE_FILE.with_name("state.json.corrupt-%d" % int(time.time()))
        os.replace(STATE_FILE, dst)
        saved = str(dst)
    except Exception:
        saved = None
    rec = {"ts": time.time(), "kind": "STATE_CORRUPT",
           "error": str(err)[:200], "preserved_as": saved,
           "note": "state.json 读取/解析失败，offset 回退草稿文件末尾；"
                   "原文件已保全，未消费草稿可能被跳过，请凭保全文件对账"}
    line = json.dumps(rec, ensure_ascii=False)
    try:
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        print(f"alert log error: {e}", file=sys.stderr, flush=True)
    print("STATE_CORRUPT " + line, file=sys.stderr, flush=True)

def load_state():
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text())
            if "sent" not in st:
                st["sent"] = {"grp-81658512": st.pop("sent_ts", [])}
            st.setdefault("proactive_ts", [])
            st.setdefault("proactive_dm_ts", [])
            return st
        except Exception as e:
            _preserve_corrupt_state(e)
    return {"offset": DRAFTS_LOG.stat().st_size if DRAFTS_LOG.exists() else 0,
            "sent": {}, "proactive_ts": [], "proactive_dm_ts": []}

def save_state(st):
    _atomic_write(STATE_FILE, json.dumps(st))

# ---- in-flight ledger (audit B-02) -------------------------------------
# One entry per candidate draft that has started processing and has not
# reached a terminal outcome yet. fsync'd before any send; removed only
# after the outcome is in sends.jsonl.
def load_inflight():
    try:
        return json.loads(INFLIGHT_FILE.read_text())
    except Exception:
        return {}

def _save_inflight(d):
    _atomic_write(INFLIGHT_FILE, json.dumps(d, ensure_ascii=False))

def inflight_mark(draft_id, summary):
    d = load_inflight()
    d[draft_id] = {"ts": time.time(), "summary": summary[:120],
                   "state": "in_flight"}
    _save_inflight(d)

def inflight_done(draft_id):
    d = load_inflight()
    if draft_id in d:
        d.pop(draft_id)
        _save_inflight(d)

def reconcile_inflight():
    """Startup: any leftover in-flight draft died before its terminal
    record. Report (stderr + sends-alerts.jsonl), never resend."""
    d = load_inflight()
    if not d:
        return 0
    for did, ent in d.items():
        rec = {"ts": time.time(), "kind": "INFLIGHT_LOST", "draft_id": did,
               "inflight_ts": ent.get("ts"), "summary": ent.get("summary"),
               "note": "进程在终态落账前退出，该草稿可能漏发；未自动重发（防重复优先）"}
        line = json.dumps(rec, ensure_ascii=False)
        try:
            with open(ALERT_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"alert log error: {e}", file=sys.stderr, flush=True)
        print("INFLIGHT_LOST " + line, file=sys.stderr, flush=True)
    _save_inflight({})
    return len(d)

def log_rec(rec):
    rec = {"ts": time.time(), **rec}
    with open(SENDS_LOG, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps(rec, ensure_ascii=False), flush=True)

# ---- 发送归属（2026-10-27 审计 A04） ---------------------------------
# sends 记录的 origin：这一稿到底是谁产的。sends 有账只证明「经
# sender 发出」，不证明「她自主生成」——人工补发同样走 sender。
# 判定只认草稿自带的真实元数据，判不出记 unknown，绝不猜（归属将是
# 成长证据的第一道闸，宁缺不错）：生产者显式 origin 字段优先；没打
# 标的按结构特征兜底（proactive 入口标记、哨兵文本、run_draft 独有
# 的 segs 字段）。人工补发只有在写草稿时自带 origin="manual" 才记
# manual，没带的落 unknown。
ORIGIN_KNOWN = {"autonomous", "manual", "heartbeat", "proactive_dm",
                "command", "anthem", "song", "botkey"}

def draft_origin(d):
    o = d.get("origin")
    if o in ORIGIN_KNOWN:
        return o
    if d.get("proactive"):
        if d.get("incoming") == "[主动冒泡]":
            return "heartbeat"
        if d.get("incoming") == "[主动搭话]":
            return "proactive_dm"
        return "unknown"
    raw = (d.get("draft_text") or "").strip()
    if raw == "[anthem]":
        return "anthem"
    if SONG_RE.match(raw):
        return "song"
    if "segs" in d:
        return "autonomous"
    return "unknown"

# ---- single outbound chokepoint: gate + segment brake ------------------
class GateClosed(RuntimeError):
    pass

class BrakeTripped(RuntimeError):
    pass

def gate_on():
    return ENABLE_FILE.exists()

def killswitch_on():
    return KILLSWITCH_FILE.exists()

def _recent_segments(st, key, now=None):
    """Save 24h of segment timestamps, return count in the last hour.
    One entry = one actual outbound message (segment), not one draft."""
    now = now or time.time()
    hist = [t for t in st["sent"].get(key, []) if now - t < 86400]
    st["sent"][key] = hist
    return sum(1 for t in hist if now - t < 3600)

def _raw_post(url, payload, timeout):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _resp_ok(resp):
    """OneBot 业务成功口径，与 handle_draft 调用方的校验一致：
    status == 'ok' 且 retcode == 0 才算这一段真发出去了。"""
    return isinstance(resp, dict) and resp.get("status") == "ok" \
        and resp.get("retcode") == 0

def _unknown_log(kind, target, key, err):
    """POST 已发出但结果未知（超时/断连）：不计已发，单独落账供事后
    对账。这类段可能已送达也可能没有，绝不混进 sent 计数。"""
    rec = {"ts": time.time(), "kind": "SEND_UNKNOWN", "via": kind,
           "target": target, "key": key, "error": str(err)[:200],
           "note": "POST 结果未知，未计入已发送段计数"}
    try:
        with open(UNKNOWN_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass
    print("SEND_UNKNOWN " + json.dumps(rec, ensure_ascii=False),
          file=sys.stderr, flush=True)

def post(kind, target, field, message, timeout=15, st=None, key=None):
    """The ONLY outbound path. Checks the master gate and the per-hour
    segment brake immediately before every POST, and ledgers the segment
    into state['sent'][key] only after business success is confirmed.

    R-03（GPT 复审 2026-10-27）：计数时机后移到业务成功之后。旧实现
    POST 一返回就计数，但 status/retcode 的成败校验在调用方——OneBot
    回一个结构完整的失败应答也会白吃 100/小时的熔断额度，语音失败
    回退文字时同一段还会重复吃。现在：_resp_ok 才计数；失败应答原样
    返回给调用方按既有逻辑处理、不计数；POST 本身抛异常（超时/断连）
    ＝结果未知，单独记 sends-unknown.jsonl、不计数、异常照常上抛。"""
    if not gate_on():
        raise GateClosed("SEND_ENABLED missing")
    if killswitch_on():
        raise GateClosed("GATE_KILLSWITCH")
    if st is not None and key is not None \
            and _recent_segments(st, key) >= BRAKE_PER_HOUR:
        raise BrakeTripped("brake:hour")
    if kind == "group":
        url, payload = f"{API_BASE}/send_group_msg", {"group_id": target, field: message}
    else:
        url, payload = f"{API_BASE}/send_private_msg", {"user_id": target, field: message}
    try:
        resp = _raw_post(url, payload, timeout)
    except Exception as e:
        _unknown_log(kind, target, key, e)
        raise
    if _resp_ok(resp) and st is not None and key is not None:
        st["sent"].setdefault(key, []).append(time.time())
        save_state(st)
    return resp

def send_message(kind, target, text, st=None, key=None):
    return post(kind, target, "message", text, 15, st, key)

def send_record(kind, target, wav_path, reply_to=None, st=None, key=None):
    """Send a local wav as a QQ voice message (OneBot record segment;
    NapCat transcodes to silk itself)."""
    msg = []
    if isinstance(reply_to, int):
        msg.append({"type": "reply", "data": {"id": str(reply_to)}})
    msg.append({"type": "record", "data": {"file": str(wav_path)}})
    return post(kind, target, "message", msg, 30, st, key)

SWALLOW_RE = re.compile(r"（（.*?））", re.S)


def strip_swallowed(text):
    """（（…）） = 她哽回去的话：发送前整段剔除，可跨行。
    未闭合的 （（ 没有配对 ）） 时正则不匹配，原样保留 —— 不误删正文。"""
    if "（（" not in text:
        return text
    return SWALLOW_RE.sub("", text)


VOICE_ONLY_FLAG = QQ_DIR / "voice-out" / "voice-only-tonight"


def _voice_only_tonight():
    try:
        return VOICE_ONLY_FLAG.exists()
    except Exception:
        return False


AUTO_VOICE_MIN = 6
AUTO_VOICE_MAX = 45
AUTO_VOICE_PER_HOUR = 3
AUTO_VOICE_MIN_GAP = 300
_AUTO_VOICE_LAST = {}
_AUTO_VOICE_HIST = {}


def _auto_voice_ok(key, visible):
    """Standing auto-voice for short plain lines (2026-10-29). Until
    today this only existed inside the voice-only-tonight branch, so
    when tonight mode was closed at 09:00 her voice went with it - the
    TTS line was healthy, the gate was the bug. Standing version keeps
    the spirit (short lines get spoken) with a per-chat throttle so a
    busy hour cannot turn into a voice flood."""
    if not (AUTO_VOICE_MIN <= len(visible) <= AUTO_VOICE_MAX):
        return False
    if "[" in visible or "http" in visible:
        return False
    now = time.time()
    if now - _AUTO_VOICE_LAST.get(key, 0) < AUTO_VOICE_MIN_GAP:
        return False
    hist = [t for t in _AUTO_VOICE_HIST.get(key, []) if now - t < 3600]
    if len(hist) >= AUTO_VOICE_PER_HOUR:
        _AUTO_VOICE_HIST[key] = hist
        return False
    hist.append(now)
    _AUTO_VOICE_HIST[key] = hist
    _AUTO_VOICE_LAST[key] = now
    return True


def render_parts(raw, manifest, key=None):
    """Split a draft into sendable parts, rendering [face:ID] tokens and
    [sticker:slug] tokens (curated library images). A line prefixed with
    [voice] is marked for speech: voice_map maps the part's index in the
    returned list to the exact text to speak (plain text only - a line
    carrying face/sticker tokens stays a text part). The part itself is
    kept as the plain text so a failed voice send can fall back to it.
    Returns (parts, reason, voice_map)."""
    raw = strip_swallowed(raw)
    if "[CQ:" in raw:
        return None, "blocked:raw_cq", {}
    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    if not parts:
        return None, "empty", {}
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS - 1] + [" ".join(parts[MAX_PARTS - 1:])]
    out = []
    voice_map = {}
    for p in parts:
        is_voice = bool(VOICE_RE.match(p))
        if is_voice:
            p = VOICE_RE.sub("", p).strip()
        p = p.replace("[voice]", "").strip()
        slugs = [s for s in STICKER_RE.findall(p) if s in manifest]
        p = STICKER_RE.sub("", p).strip()
        def _sub(m):
            fid = int(m.group(1))
            return f"[CQ:face,id={fid}]" if fid in FACE_ALLOW else ""
        p = FACE_RE.sub(_sub, p).strip()
        visible = re.sub(r"\[CQ:face,id=\d+\]", "", p)
        if visible or "[CQ:face" in p:
            if len(visible) > MAX_LEN:
                return None, f"too_long:{len(visible)}", {}
            if is_voice and visible == p:
                voice_map[len(out)] = visible
            elif _voice_only_tonight() and visible and len(visible) >= 6:
                # Voice-only tonight: a line carrying [face:] tokens used
                # to stay text (visible != payload). Speak its visible
                # text instead; the face is dropped for that line.
                voice_map[len(out)] = visible
            elif not voice_map and visible and _auto_voice_ok(key, visible):
                # Standing auto-voice: first eligible short line of the
                # draft is spoken, the rest stays text (one line a round).
                voice_map[len(out)] = visible
            out.append(p)
        for s in slugs:
            fpath = STICKERS_DIR / "library" / manifest[s]["file"]
            if fpath.exists() and len(out) < MAX_PARTS + 1:
                out.append(f"[CQ:image,file=file://{fpath}]")
    return (out or None), ("" if out else "empty"), voice_map

def build_plan(kind, d):
    """Expand a draft into its outbound segment list without sending.
    op = (channel, payload, reply_to); channel is "text" or "record".
    [anthem] / [song:*] are expanded HERE so the unified check sees their
    real segment count (audit B-03) and they can no longer bypass it.
    Returns (ops, voice_map, kind_tag) or (None, reason, "")."""
    raw = (d.get("draft_text") or "").strip()
    if raw == "[anthem]":
        ops = [("record", ANTHEM_WAV, d.get("reply_to"))]
        ent = (load_manifest().get(ANTHEM_STICKER_SLUG) or {})
        fpath = STICKERS_DIR / "library" / (ent.get("file") or "")
        if ent and ent.get("file") and fpath.exists():
            ops.append(("text", f"[CQ:image,file=file://{fpath}]", None))
        ops.append(("text", ANTHEM_TEXT, None))
        return ops, {}, "anthem"
    m = SONG_RE.match(raw)
    if m:
        slug = m.group(1)
        info = load_song_index().get(slug) or {}
        title = info.get("title") or slug
        wav = SONGS_DIR / (info.get("file") or "")
        if info.get("file") and wav.exists():
            return [("record", wav, d.get("reply_to")),
                    ("text", f"《{title}》", None)], {}, "song"
        return [("text", "这首没录", None)], {}, "song"
    parts, reason, voice_map = render_parts(raw, load_manifest(), d.get("key"))
    if parts is None:
        return None, reason, ""
    return [("text", p, None) for p in parts], voice_map, "text"

def check_candidate(key, kind, d, st):
    """Validate one (possibly multi-segment) draft. Returns
    (ok, reason, ops, voice_map, kind_tag)."""
    raw = d.get("draft_text") or ""
    low = raw.lower()
    for b in BLOCK_SUBSTRINGS:
        if b.lower() in low:
            return False, f"blocked:{b}", None, {}, ""
    ops, voice_map, kind_tag = build_plan(kind, d)
    if ops is None:
        return False, voice_map, None, {}, ""
    now = time.time()
    if d.get("proactive"):
        if kind == "group":
            hist = [t for t in st.get("proactive_ts", []) if now - t < 86400]
            st["proactive_ts"] = hist
            if hist and now - hist[-1] < PROACTIVE_MIN_INTERVAL:
                return False, "proactive:min_interval", None, {}, ""
            if len(hist) >= PROACTIVE_MAX_PER_DAY:
                return False, "proactive:day", None, {}, ""
        elif key == "dm-1480760876":
            # Proactive DM to the master (2026-10-26, opt-in feature):
            # the proactive_dm daemon gates on the ENABLED flag and idle
            # time before drafting; re-check the flag here so switching
            # it off also kills openers already queued. Other DMs never.
            if not (QQ_DIR / "proactive-dm" / "ENABLED").exists():
                return False, "proactive:dm_disabled", None, {}, ""
            hist = [t for t in st.get("proactive_dm_ts", []) if now - t < 86400]
            st["proactive_dm_ts"] = hist
            if hist and now - hist[-1] < PROACTIVE_MIN_INTERVAL:
                return False, "proactive:dm_min_interval", None, {}, ""
            if len(hist) >= PROACTIVE_MAX_PER_DAY:
                return False, "proactive:dm_day", None, {}, ""
        else:
            return False, "proactive:dm_not_allowed", None, {}, ""
    # Segment-based brake: st['sent'] now holds one timestamp per actual
    # outbound message, so this is a real segments/hour count. Forecast
    # this draft's segment count too, so a 3-part reply cannot start at 99.
    if _recent_segments(st, key, now) + len(ops) > BRAKE_PER_HOUR:
        return False, "brake:hour", None, {}, ""
    return True, "", ops, voice_map, kind_tag

def _sent_history(key, now):
    """Her previously SENT drafts in this conversation (post-eraser
    truth, from the audit log): (last-3 list, last-24h list)."""
    recent, day = [], []
    try:
        rows = []
        for line in open(SENDS_LOG, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("result") == "SENT" and r.get("key") == key \
                    and (r.get("draft") or "").strip():
                rows.append(r)
        recent = [r["draft"] for r in rows[-3:]]
        day = [r["draft"] for r in rows
               if now - (r.get("ts") or 0) <= 86400]
    except Exception:
        pass
    return recent, day


def handle_draft(draft_id, d, route, st):
    """Process one candidate to a terminal outcome. The caller advances the
    offset and clears the in-flight entry only after this returns."""
    kind, target = route
    base = {"key": d["key"], "incoming": d.get("incoming"),
            "draft": d.get("draft_text"),
            "proactive": bool(d.get("proactive")),
            "origin": draft_origin(d),
            "draft_id": draft_id,
            # 第 2 批（2-1 / S-04）：事务 ID 从草稿记录原样透传，与 events
            # 的 tx 对上账；非事件源头草稿（心跳等）无 tx 时为 None。
            "tx": d.get("tx")}
    _bp = d.get("batch_primary")
    if _bp:
        try:
            DUP_CLAIMS_DIR.mkdir(parents=True, exist_ok=True)
            _fd = os.open(DUP_CLAIMS_DIR / str(_bp),
                          os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(_fd)
        except FileExistsError:
            log_rec({**base, "result": "DUP_BLOCKED",
                     "reason": "batch_primary already claimed"})
            return
        except Exception:
            pass
    summary = re.sub(r"\s+", " ", (d.get("draft_text") or "")).strip()
    inflight_mark(draft_id, summary)
    segments = 0
    part_ids = []   # 全部段的 message_id（D-01 根治）；提前初始化，异常路径也要能落账
    try:
        if not gate_on():
            log_rec({**base, "result": "GATE_OFF"}); return
        if apply_rubber is not None and RUBBER_FILE.exists():
            # Outbound eraser (2026-10-27): deterministic,
            # delete-only frequency control (self-reference cap,
            # face cooldown, tail dedup). Audit keeps the final
            # text in "draft" and the original in "draft_orig".
            try:
                _recent, _day = _sent_history(d["key"], time.time())
                _new, _rlog = apply_rubber(
                    d.get("draft_text") or "", _recent, _day)
                if _rlog:
                    base["draft_orig"] = d.get("draft_text")
                    base["rubber"] = _rlog
                    base["draft"] = _new
                    d = {**d, "draft_text": _new}
            except Exception:
                pass
        ok, reason, ops, voice_map, kind_tag = check_candidate(
            d["key"], kind, d, st)
        if d.get("gate_downgraded"):
            # Downgraded send (unified gate, 2026-11-01): text only -
            # no voice attempts; the bridge already forced quote off.
            voice_map = {}
        if not ok:
            tag = ("PROACTIVE_LIMITED" if reason.startswith("proactive:")
                   else "RATE_LIMITED" if reason.startswith(("rate:", "brake:"))
                   else "HELD")
            log_rec({**base, "result": tag, "reason": reason}); return
        typo = (plan_typo([op[1] for op in ops], skip=set(voice_map))
                if (kind_tag == "text" and kind == "group"
                    and not d.get("proactive")) else None)
        if typo:
            pi, wrong, _right = typo
            ch, payload, reply_to = ops[pi]
            ops[pi] = (ch, payload.replace(_right, wrong, 1), reply_to)
        last_id = None
        voice_sent = []
        for i, (channel, payload, reply_to) in enumerate(ops):
            if i == 0:
                vis = len(re.sub(r"\[CQ:[^\]]+\]", "", str(payload)))
                time.sleep(min(0.6 + 0.04 * vis, 3.0) * random.uniform(0.7, 1.3))
            mid = None
            spoken = voice_map.get(i) if channel == "text" else None
            if spoken is not None:
                # Voice attempt. Any failure (TTS down, OneBot error)
                # leaves mid None and the part falls through to the
                # plain-text send below - a voice problem must never
                # eat a reply.
                wav = synth_voice(spoken)
                if wav is not None:
                    try:
                        resp = send_record(
                            kind, target, wav,
                            reply_to=reply_to if i == 0 else None,
                            st=st, key=d["key"])
                        if resp.get("status") != "ok" or resp.get("retcode") != 0:
                            raise RuntimeError(f"onebot: {str(resp)[:200]}")
                        mid = (resp.get("data") or {}).get("message_id")
                        segments += 1
                        voice_sent.append({"index": i, "text": spoken,
                                           "file": str(wav)})
                    except (GateClosed, BrakeTripped):
                        raise
                    except Exception:
                        mid = None
            if mid is None:
                if channel == "record":
                    resp = send_record(kind, target, payload, reply_to=reply_to,
                                       st=st, key=d["key"])
                else:
                    text = payload
                    if i == 0 and isinstance(d.get("reply_to"), int):
                        # Quote the message being answered, like a human
                        # tapping "reply" (sender-made CQ).
                        text = f"[CQ:reply,id={d['reply_to']}]" + text
                    resp = send_message(kind, target, text, st=st, key=d["key"])
                if resp.get("status") != "ok" or resp.get("retcode") != 0:
                    raise RuntimeError(f"onebot: {str(resp)[:200]}")
                mid = (resp.get("data") or {}).get("message_id")
                segments += 1
            last_id = mid
            part_ids.append(mid)
            if i < len(ops) - 1:
                nxt = len(re.sub(r"\[CQ:[^\]]+\]", "", str(ops[i + 1][1])))
                time.sleep(max(0.8, min(0.7 + 0.045 * nxt, 3.2)) * random.uniform(0.8, 1.2))
        now = time.time()
        if d.get("proactive"):
            if d["key"] == "dm-1480760876":
                st.setdefault("proactive_dm_ts", []).append(now)
            else:
                st.setdefault("proactive_ts", []).append(now)
            save_state(st)
        audit = {**base, "result": "SENT", "parts": segments,
                 "segments": segments, "brake_units": "segments",
                 "message_id": last_id,
                 # D-01 根治（第 2 批）：全部段的 message_id 都落账——引用
                 # 解析靠它按确切 id 命中任意一段（尤其歌曲的语音段），
                 # 只存 last_id 时引语音段必然落空（11:26 《年轮》事故）。
                 "message_ids": part_ids}
        if kind_tag != "text":
            audit["kind"] = kind_tag
        if voice_sent:
            audit["kind"] = ("voice" if len(voice_sent) == len(ops)
                             else "mixed")
            audit["voice"] = voice_sent
        log_rec(audit)
        if typo and part_ids[typo[0]]:
            try:
                time.sleep(random.uniform(1.8, 4.5))
                send_message(kind, target,
                             f"[CQ:reply,id={part_ids[typo[0]]}]*{typo[2]}",
                             st=st, key=d["key"])
                log_rec({**base, "result": "TYPO_FIX", "fix": typo[2],
                         "segments": 1, "brake_units": "segments"})
            except Exception:
                pass
    except GateClosed as e:
        log_rec({**base, "result": "GATE_OFF",
                 "reason": ("GATE_KILLSWITCH" if "KILLSWITCH" in str(e)
                            else "gate_closed_midstream"),
                 "segments_sent": segments, "message_ids": part_ids})
    except BrakeTripped:
        log_rec({**base, "result": "RATE_LIMITED", "reason": "brake:hour",
                 "segments_sent": segments, "message_ids": part_ids})
    except Exception as e:
        log_rec({**base, "result": "FAILED", "error": str(e)[:300],
                 "segments_sent": segments, "message_ids": part_ids})
    finally:
        inflight_done(draft_id)


def pump(st):
    """One pass over the drafts log. Returns candidate count handled.
    Only complete lines are consumed; the offset moves one line at a time,
    after that line reached a terminal outcome."""
    if not DRAFTS_LOG.exists():
        return 0
    size = DRAFTS_LOG.stat().st_size
    if size < st["offset"]:
        st["offset"] = 0
        save_state(st)
    if size == st["offset"]:
        return 0
    with open(DRAFTS_LOG, "rb") as f:
        f.seek(st["offset"])
        chunk = f.read()
    pos = st["offset"]
    handled = 0
    for raw_line in chunk.split(b"\n")[:-1]:
        end = pos + len(raw_line) + 1
        try:
            d = json.loads(raw_line.decode("utf-8", "ignore"))
        except Exception:
            d = None
        route = ROUTES.get(d.get("key")) if d else None
        if route and d.get("decision") == "REPLY":
            did = "d%d-%s" % (pos, hashlib.sha1(raw_line).hexdigest()[:10])
            handle_draft(did, d, route, st)
            handled += 1
        st["offset"] = end
        save_state(st)
        pos = end
    return handled


def main():
    st = load_state()
    save_state(st)
    left = reconcile_inflight()
    print(f"sender up: routes={list(ROUTES)} "
          f"gate={'ON' if gate_on() else 'OFF'} "
          f"tail_offset={st['offset']} inflight_lost={left}", flush=True)
    while True:
        try:
            pump(st)
            time.sleep(2)
        except Exception as e:
            print(f"sender loop error: {e}", flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()
