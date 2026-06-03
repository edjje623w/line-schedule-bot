import os
import re
from datetime import datetime, date, timedelta
from urllib.parse import urlparse
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)
import threading
import time
import pg8000.native

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))

USER_ID_1 = os.environ.get("USER_ID_1", "USER_ID_1")
USER_ID_2 = os.environ.get("USER_ID_2", "USER_ID_2")
USER_NAME_1 = os.environ.get("USER_NAME_1", "阿馨")
USER_NAME_2 = os.environ.get("USER_NAME_2", "阿虎")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

USER_NAMES = {USER_ID_1: USER_NAME_1, USER_ID_2: USER_NAME_2}
ALL_USER_IDS = [USER_ID_1, USER_ID_2]
user_state = {}

# ── 資料庫 ───────────────────────────────────────────────────────────
def get_conn():
    url = urlparse(DATABASE_URL)
    return pg8000.native.Connection(
        user=url.username, password=url.password,
        host=url.hostname, port=url.port or 5432,
        database=url.path.lstrip("/"), ssl_context=True
    )

def db_run(sql, params=None, fetch=False):
    conn = get_conn()
    try:
        if params:
            # pg8000 uses :param_name syntax with keyword args
            named_sql = sql
            named_params = {}
            for i, val in enumerate(params, 1):
                named_sql = named_sql.replace(f":{i}", f":p{i}")
                named_params[f"p{i}"] = val
            result = conn.run(named_sql, **named_params)
        else:
            result = conn.run(sql)
        return result if fetch else None
    finally:
        conn.close()

def init_db():
    db_run("""
        CREATE TABLE IF NOT EXISTS schedules (
            id SERIAL PRIMARY KEY,
            owner TEXT NOT NULL,
            category TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_date_end TEXT,
            event_time TEXT,
            description TEXT NOT NULL,
            url TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reminded_day_before INTEGER DEFAULT 0,
            reminded_same_day INTEGER DEFAULT 0,
            reminded_three_days INTEGER DEFAULT 0
        )
    """)

# ── 工具函式 ─────────────────────────────────────────────────────────
def get_user_name(user_id):
    return USER_NAMES.get(user_id, "你")

def get_other_id(user_id):
    for uid in ALL_USER_IDS:
        if uid != user_id:
            return uid
    return None

def weekday_str(d):
    return ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]

def format_date(d):
    return f"{d.month}/{d.day}（週{weekday_str(d)}）"

def format_date_range(d_start, d_end):
    if d_end and d_end != d_start:
        return f"{d_start.month}/{d_start.day}（週{weekday_str(d_start)}）－{d_end.month}/{d_end.day}（週{weekday_str(d_end)}）"
    return format_date(d_start)

def fmt_list(row):
    d_start = datetime.strptime(row[3], "%Y-%m-%d").date()
    d_end = datetime.strptime(row[4], "%Y-%m-%d").date() if row[4] else None
    date_str = format_date_range(d_start, d_end)
    time_str = f" {row[5]}" if row[5] else ""
    url_str = " 🔗" if row[7] else ""
    return f"{date_str}{time_str} {row[6]}{url_str}"

def fmt_reminder(row):
    d_start = datetime.strptime(row[3], "%Y-%m-%d").date()
    d_end = datetime.strptime(row[4], "%Y-%m-%d").date() if row[4] else None
    date_str = format_date_range(d_start, d_end)
    time_str = f" {row[5]}" if row[5] else ""
    url_str = f"\n🔗 {row[7]}" if row[7] else ""
    return f"{date_str}{time_str} {row[6]}{url_str}"

def qr_yes_no():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="是", text="是")),
        QuickReplyButton(action=MessageAction(label="否", text="否")),
    ])

def qr_numbers(n):
    items = [QuickReplyButton(action=MessageAction(label=str(i), text=str(i))) for i in range(1, min(n+1, 12))]
    items.append(QuickReplyButton(action=MessageAction(label="取消", text="取消")))
    return QuickReply(items=items)

# ── 解析日期時間 ──────────────────────────────────────────────────────
def parse_schedule(text):
    today = date.today()
    year = today.year
    url = None

    url_match = re.search(r'https?://\S+', text)
    if url_match:
        url = url_match.group(0)
        text = text[:url_match.start()].strip()

    # 連續日期
    range_match = re.match(r'^(\d{1,2})[/\-](\d{1,2})\s*[到\-～~]\s*(\d{1,2})\s*', text)
    if range_match:
        month, day_s, day_e = int(range_match.group(1)), int(range_match.group(2)), int(range_match.group(3))
        ed = date(year, month, day_s)
        if ed < today:
            ed = date(year+1, month, day_s)
            ee = date(year+1, month, day_e)
        else:
            ee = date(year, month, day_e)
        desc = text[range_match.end():].strip()
        if not desc: return None
        if ed < today: return {"error": "past"}
        return {"date": ed.strftime("%Y-%m-%d"), "date_end": ee.strftime("%Y-%m-%d"), "time": None, "description": desc, "url": url}

    event_date = None
    for word, delta in {"今天": 0, "明天": 1, "後天": 2, "大後天": 3}.items():
        if text.startswith(word):
            event_date = today + timedelta(days=delta)
            text = text[len(word):].strip()
            break

    if event_date is None:
        m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\s*", text)
        if m:
            event_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            text = text[m.end():]
        else:
            m = re.match(r"^(\d{1,2})[/\-](\d{1,2})\s*", text)
            if m:
                mo, dy = int(m.group(1)), int(m.group(2))
                event_date = date(year, mo, dy)
                if event_date < today:
                    event_date = date(year+1, mo, dy)
                text = text[m.end():]

    if event_date is None: return None
    if event_date < today: return {"error": "past"}

    event_time = None
    for pattern, extractor in [
        (r"^(下午|晚上)\s*(\d{1,2})[點:：](\d{2})\s*", lambda m: (int(m.group(2))+12 if int(m.group(2))<12 else int(m.group(2)), int(m.group(3)))),
        (r"^(下午|晚上)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2))+12 if int(m.group(2))<12 else int(m.group(2)), 0)),
        (r"^(早上|上午|早)\s*(\d{1,2})[點:：](\d{2})\s*", lambda m: (int(m.group(2)), int(m.group(3)))),
        (r"^(早上|上午|早)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2)), 0)),
        (r"^(\d{1,2})[::：點](\d{2})\s*", lambda m: (int(m.group(1)), int(m.group(2)))),
        (r"^(\d{1,2})\s*點(\d{2})?\s*", lambda m: (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)),
        (r"^(\d{1,2})\s*(?=[\u4e00-\u9fff])", lambda m: (int(m.group(1)), 0) if 0 <= int(m.group(1)) <= 23 else None),
    ]:
        m = re.match(pattern, text)
        if m:
            r = extractor(m)
            if r and 0 <= r[0] <= 23:
                event_time = f"{r[0]:02d}:{r[1]:02d}"
                text = text[m.end():]
                break

    desc = text.strip()
    if not desc: return None
    return {"date": event_date.strftime("%Y-%m-%d"), "date_end": None, "time": event_time, "description": desc, "url": url}

# ── 多行解析 ─────────────────────────────────────────────────────────
def parse_multi(text, default_owner_id, default_owner_name):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    results = []
    errors = []
    for line in lines:
        owner_id = default_owner_id
        owner_name = default_owner_name
        for uid, name in USER_NAMES.items():
            if line.startswith(name+" ") or line.startswith(name+"　"):
                owner_id = uid
                owner_name = name
                line = line[len(name):].strip()
                break
        parsed = parse_schedule(line)
        if parsed:
            if parsed.get("error") == "past":
                errors.append(f"❌ 日期已過：{line}")
            else:
                results.append({"owner_id": owner_id, "owner_name": owner_name, "parsed": parsed})
        else:
            errors.append(f"❓ 看不懂：{line}")
    return results, errors

# ── 查詢行程 ──────────────────────────────────────────────────────────
def get_schedules(start_date, end_date, owner=None):
    if owner:
        rows = db_run(
            "SELECT * FROM schedules WHERE event_date <= :1 AND (event_date_end >= :2 OR (event_date_end IS NULL AND event_date >= :3)) AND owner = :4 ORDER BY event_date, event_time NULLS LAST",
            (end_date, start_date, start_date, owner), fetch=True)
    else:
        rows = db_run(
            "SELECT * FROM schedules WHERE event_date <= :1 AND (event_date_end >= :2 OR (event_date_end IS NULL AND event_date >= :3)) ORDER BY event_date, event_time NULLS LAST",
            (end_date, start_date, start_date), fetch=True)
    return rows or []

def format_list(rows, title):
    if not rows:
        return f"📅 {title}\n\n（這段時間沒有行程）"
    groups = {USER_NAME_1: [], USER_NAME_2: [], "共同": []}
    for row in rows:
        line = fmt_list(row)
        if row[2] == "共同": groups["共同"].append(line)
        elif row[1] == USER_ID_1: groups[USER_NAME_1].append(line)
        elif row[1] == USER_ID_2: groups[USER_NAME_2].append(line)
    lines = [f"📅 {title}\n"]
    for name in [USER_NAME_1, USER_NAME_2]:
        if groups[name]:
            lines.append(name); lines.extend(groups[name]); lines.append("")
    if groups["共同"]:
        lines.append(f"👫 {USER_NAME_1}+{USER_NAME_2}"); lines.extend(groups["共同"]); lines.append("")
    return "\n".join(lines).strip()

def check_conflict(user_id, event_date, event_time):
    if not event_time: return None
    other_id = get_other_id(user_id)
    rows = db_run("SELECT * FROM schedules WHERE owner = :1 AND event_date = :2 AND event_time = :3",
                  (other_id, event_date, event_time), fetch=True)
    return rows[0] if rows else None

def save_schedule(owner_id, category, parsed, created_by):
    db_run("""INSERT INTO schedules (owner, category, event_date, event_date_end, event_time, description, url, created_by, created_at)
              VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9)""",
           (owner_id, category, parsed["date"], parsed.get("date_end"),
            parsed["time"], parsed["description"], parsed.get("url"),
            created_by, datetime.now().isoformat()))

def do_save_one(created_by, owner_id, category, parsed, owner_name):
    save_schedule(owner_id, category, parsed, created_by)
    d_s = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
    d_e = datetime.strptime(parsed["date_end"], "%Y-%m-%d").date() if parsed.get("date_end") else None
    date_str = format_date_range(d_s, d_e)
    time_str = f" {parsed['time']}" if parsed["time"] else ""
    url_str = f"\n🔗 {parsed['url']}" if parsed.get("url") else ""
    if category == "共同":
        other_id = get_other_id(created_by)
        if other_id:
            try:
                line_bot_api.push_message(other_id, TextSendMessage(
                    text=f"📅 {get_user_name(created_by)}新增了共同行程\n{date_str}{time_str} {parsed['description']}{url_str}"))
            except Exception as e:
                print(f"Notify error: {e}")
        return f"共同｜{date_str}{time_str} {parsed['description']}{url_str}"
    return f"{owner_name}｜{date_str}{time_str} {parsed['description']}{url_str}"

# ── 主訊息處理 ────────────────────────────────────────────────────────
def handle_message(user_id, text):
    text = text.strip()
    today = date.today()

    if user_id in user_state:
        state = user_state[user_id]

        if state["step"] == "wait_category":
            data = state["data"]
            if text in ["是", "1"]:
                del user_state[user_id]
                category = "共同"
                items = data["items"]
                saved = []
                for item in items:
                    if item["parsed"]["time"]:
                        conflict = check_conflict(item["owner_id"], item["parsed"]["date"], item["parsed"]["time"])
                        if conflict:
                            # 有衝突還是存，但提示
                            result = do_save_one(user_id, item["owner_id"], category, item["parsed"], item["owner_name"])
                            saved.append(f"⚠️ {result}（與{get_user_name(get_other_id(user_id))}的行程衝突）")
                            continue
                    result = do_save_one(user_id, item["owner_id"], category, item["parsed"], item["owner_name"])
                    saved.append(result)
                return (f"✅ 已新增 {len(saved)} 筆共同行程\n\n" + "\n".join(saved), None)
            elif text in ["否", "2"]:
                del user_state[user_id]
                items = data["items"]
                saved = []
                for item in items:
                    result = do_save_one(user_id, item["owner_id"], "個人", item["parsed"], item["owner_name"])
                    saved.append(result)
                return (f"✅ 已新增 {len(saved)} 筆行程\n\n" + "\n".join(saved), None)
            else:
                del user_state[user_id]
                return ("已取消新增。", None)

        if state["step"] == "wait_delete":
            rows = state["data"]["rows"]
            if text.isdigit() and 1 <= int(text) <= len(rows):
                row = rows[int(text)-1]
                db_run("DELETE FROM schedules WHERE id = :1", (row[0],))
                del user_state[user_id]
                return (f"✅ 已刪除\n{fmt_list(row)}", None)
            del user_state[user_id]
            return ("已取消刪除。", None)

    # ── 查詢指令 ──────────────────────────────────────────────────────
    if any(kw in text for kw in ["明天行程", "查明天"]):
        tomorrow = today + timedelta(days=1)
        rows = get_schedules(tomorrow.strftime("%Y-%m-%d"), tomorrow.strftime("%Y-%m-%d"))
        return (format_list(rows, f"明天行程（{tomorrow.month}/{tomorrow.day}）"), None)

    if any(kw in text for kw in ["本週行程", "查本週", "這週行程"]):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return (format_list(rows, f"本週行程（{start.month}/{start.day}～{end.month}/{end.day}）"), None)

    if any(kw in text for kw in ["本月行程", "查本月"]):
        start = today.replace(day=1)
        end = ((start + timedelta(days=32)).replace(day=1)) - timedelta(days=1)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return (format_list(rows, f"本月行程（{start.month}月）"), None)

    if any(kw in text for kw in ["未來行程", "查未來"]):
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31")
        return (format_list(rows, "未來所有行程"), None)

    if text == "我的":
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=user_id)
        return (format_list(rows, f"{get_user_name(user_id)}的行程"), None)

    if text in ["他的", "她的"]:
        other_id = get_other_id(user_id)
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=other_id)
        return (format_list(rows, f"{get_user_name(other_id)}的行程"), None)

    if f"{USER_NAME_1}行程" in text or f"查{USER_NAME_1}" in text:
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=USER_ID_1)
        return (format_list(rows, f"{USER_NAME_1}的行程"), None)

    if f"{USER_NAME_2}行程" in text or f"查{USER_NAME_2}" in text:
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=USER_ID_2)
        return (format_list(rows, f"{USER_NAME_2}的行程"), None)

    if text == "刪除":
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31")
        if not rows:
            return ("目前沒有任何未來行程。", None)
        user_state[user_id] = {"step": "wait_delete", "data": {"rows": rows}}
        lines = ["請選擇要刪除哪筆行程：\n"]
        for i, row in enumerate(rows, 1):
            cat_str = "（共同）" if row[2] == "共同" else ""
            lines.append(f"{i}. {get_user_name(row[1])}｜{fmt_list(row)}{cat_str}")
        lines.append("\n輸入數字選擇，其他輸入取消")
        return ("\n".join(lines), qr_numbers(len(rows)))

    if text in ["幫助", "help", "?", "？", "說明", "指令"]:
        return (get_help_text(), None)

    # ── 多行新增行程 ──────────────────────────────────────────────────
    owner_id = user_id
    owner_name = get_user_name(user_id)

    results, errors = parse_multi(text, owner_id, owner_name)

    if results:
        # 預覽將新增的行程
        preview_lines = []
        for item in results:
            p = item["parsed"]
            d_s = datetime.strptime(p["date"], "%Y-%m-%d").date()
            d_e = datetime.strptime(p["date_end"], "%Y-%m-%d").date() if p.get("date_end") else None
            date_str = format_date_range(d_s, d_e)
            time_str = f" {p['time']}" if p["time"] else ""
            preview_lines.append(f"{item['owner_name']}｜{date_str}{time_str} {p['description']}")

        error_msg = ""
        if errors:
            error_msg = "\n\n⚠️ 以下無法解析：\n" + "\n".join(errors)

        user_state[user_id] = {"step": "wait_category", "data": {"items": results}}
        preview = "\n".join(preview_lines)
        return (f"準備新增以下行程：\n\n{preview}{error_msg}\n\n這些是共同行程嗎？", qr_yes_no())

    return ("😅 我看不太懂，試試這樣輸入：\n\n7/15 11:00 染頭髮\n7/15 11染頭髮\n明天 下午3點 看牙醫\n6/13-20 富國島\n"
            f"{USER_NAME_2} 7/20 開會\n\n可以一次輸入多行，每行一筆行程\n\n輸入「幫助」查看完整指令", None)

def get_help_text():
    return ("📖 使用說明\n\n【新增行程（可多行）】\n  7/15 11:00 染頭髮\n  7/15 11染頭髮\n  明天 下午3點 看牙醫\n"
            "  6/13-20 富國島（連續行程）\n"
            f"  {USER_NAME_2} 7/20 開會（幫對方新增）\n  7/15 10:00 繳費 https://xxx.com\n\n"
            f"【查詢】\n  明天行程 / 本週行程\n  未來行程\n  {USER_NAME_1}行程 / {USER_NAME_2}行程\n  我的 / 他的\n\n"
            "【刪除】輸入「刪除」選擇要刪的行程\n\n【提醒時間】\n  3天前（超過7天的行程）\n  前一天晚上 11 點\n  當天早上 9 點")

# ── Webhook ──────────────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def message_text(event):
    user_id = event.source.user_id
    text = event.message.text
    print(f"User ID: {user_id}, Message: {text}")
    reply_text, quick_reply = handle_message(user_id, text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text, quick_reply=quick_reply))

# ── 提醒排程 ──────────────────────────────────────────────────────────
def build_reminder_msg(rows, recipient_id):
    if not rows: return None
    recipient_name = get_user_name(recipient_id)
    other_name = get_user_name(get_other_id(recipient_id))
    groups = {recipient_name: [], other_name: [], "共同": []}
    for row in rows:
        line = fmt_reminder(row)
        if row[2] == "共同": groups["共同"].append(line)
        elif row[1] == recipient_id: groups[recipient_name].append(line)
        else: groups[other_name].append(line)
    if not any(groups.values()): return None
    lines = []
    for name in [recipient_name, other_name]:
        if groups[name]:
            lines.append(name); lines.extend(groups[name]); lines.append("")
    if groups["共同"]:
        lines.append(f"👫 {USER_NAME_1}+{USER_NAME_2}"); lines.extend(groups["共同"]); lines.append("")
    return "\n".join(lines).strip()

def send_reminders():
    from datetime import timezone
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    three_days_later = today + timedelta(days=3)

    if now.hour == 23 and now.minute < 10:
        rows = db_run("SELECT * FROM schedules WHERE event_date = :1 AND reminded_three_days = 0 AND (event_date::date - :2::date) > 7 ORDER BY event_time NULLS LAST",
                      (three_days_later.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")), fetch=True) or []
        for row in rows:
            line = fmt_reminder(row)
            msg = f"⏰ 3天後行程提醒\n\n{'👫 '+USER_NAME_1+'+'+USER_NAME_2 if row[2]=='共同' else get_user_name(row[1])}\n{line}"
            for uid in ALL_USER_IDS:
                try: line_bot_api.push_message(uid, TextSendMessage(text=msg))
                except Exception as e: print(f"Push error: {e}")
            db_run("UPDATE schedules SET reminded_three_days = 1 WHERE id = :1", (row[0],))

    if now.hour == 23 and now.minute < 10:
        rows = db_run("SELECT * FROM schedules WHERE event_date = :1 AND reminded_day_before = 0 ORDER BY event_time NULLS LAST",
                      (tomorrow.strftime("%Y-%m-%d"),), fetch=True) or []
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    try: line_bot_api.push_message(uid, TextSendMessage(text=f"⏰ 明天行程提醒\n\n{msg}"))
                    except Exception as e: print(f"Push error: {e}")
            for row in rows:
                db_run("UPDATE schedules SET reminded_day_before = 1 WHERE id = :1", (row[0],))

    if now.hour == 9 and now.minute < 10:
        rows = db_run("SELECT * FROM schedules WHERE event_date = :1 AND reminded_same_day = 0 ORDER BY event_time NULLS LAST",
                      (today.strftime("%Y-%m-%d"),), fetch=True) or []
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    try: line_bot_api.push_message(uid, TextSendMessage(text=f"‼️別忘了今天要做的事‼️\n\n{msg}"))
                    except Exception as e: print(f"Push error: {e}")
            for row in rows:
                db_run("UPDATE schedules SET reminded_same_day = 1 WHERE id = :1", (row[0],))

def reminder_loop():
    while True:
        try: send_reminders()
        except Exception as e: print(f"Reminder error: {e}")
        time.sleep(600)

# ── 啟動 ──────────────────────────────────────────────────────────────
init_db()
t = threading.Thread(target=reminder_loop, daemon=True)
t.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
