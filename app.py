import os
import re
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)
import threading
import time

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))

USER_ID_1 = os.environ.get("USER_ID_1", "USER_ID_1")
USER_ID_2 = os.environ.get("USER_ID_2", "USER_ID_2")
USER_NAME_1 = os.environ.get("USER_NAME_1", "阿馨")
USER_NAME_2 = os.environ.get("USER_NAME_2", "阿虎")

USER_NAMES = {
    USER_ID_1: USER_NAME_1,
    USER_ID_2: USER_NAME_2,
}

ALL_USER_IDS = [USER_ID_1, USER_ID_2]

DB_PATH = "schedules.db"

user_state = {}

# ── 資料庫 ───────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL,
            category TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_time TEXT,
            description TEXT NOT NULL,
            url TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reminded_day_before INTEGER DEFAULT 0,
            reminded_same_day INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH)

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

def format_row_for_list(row):
    event_time = row[4]
    description = row[5]
    url = row[6]
    time_str = f"{event_time} " if event_time else ""
    url_str = " 🔗" if url else ""
    return f"{time_str}{description}{url_str}"

def format_row_for_reminder(row):
    event_time = row[4]
    description = row[5]
    url = row[6]
    time_str = f"{event_time} " if event_time else ""
    url_str = f"\n🔗 {url}" if url else ""
    return f"{time_str}{description}{url_str}"

def quick_reply_yes_no():
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label="是", text="是")),
        QuickReplyButton(action=MessageAction(label="否", text="否")),
    ])

def quick_reply_numbers(n):
    items = [QuickReplyButton(action=MessageAction(label=str(i), text=str(i))) for i in range(1, min(n+1, 14))]
    return QuickReply(items=items)

# ── 解析日期時間 ──────────────────────────────────────────────────────
def parse_schedule(text):
    today = date.today()
    year = today.year
    event_date = None
    event_time = None
    url = None

    url_match = re.search(r'https?://\S+', text)
    if url_match:
        url = url_match.group(0)
        text = text[:url_match.start()].strip()

    relative = {"今天": 0, "明天": 1, "後天": 2, "大後天": 3}
    for word, delta in relative.items():
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
                month, day = int(m.group(1)), int(m.group(2))
                event_date = date(year, month, day)
                if event_date < today:
                    event_date = date(year + 1, month, day)
                text = text[m.end():]

    if event_date is None:
        return None

    if event_date < today:
        return {"error": "past"}

    # 時間解析（支援純數字如 9、11、14）
    time_patterns = [
        (r"^(下午|晚上)\s*(\d{1,2})[點::](\d{2})\s*", lambda m: (int(m.group(2)) + 12 if int(m.group(2)) < 12 else int(m.group(2)), int(m.group(3)))),
        (r"^(下午|晚上)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2)) + 12 if int(m.group(2)) < 12 else int(m.group(2)), 0)),
        (r"^(早上|上午|早)\s*(\d{1,2})[點::](\d{2})\s*", lambda m: (int(m.group(2)), int(m.group(3)))),
        (r"^(早上|上午|早)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2)), 0)),
        (r"^(\d{1,2})[::點](\d{2})\s*", lambda m: (int(m.group(1)), int(m.group(2)))),
        (r"^(\d{1,2})\s*點(\d{2})?\s*", lambda m: (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)),
        # 純數字時間：後面接空格或中文，數字在 0-23 之間
        (r"^(\d{1,2})\s+(?=[\u4e00-\u9fff])", lambda m: (int(m.group(1)), 0) if 0 <= int(m.group(1)) <= 23 else None),
    ]
    for pattern, extractor in time_patterns:
        m = re.match(pattern, text)
        if m:
            result = extractor(m)
            if result is None:
                continue
            h, mi = result
            if 0 <= h <= 23:
                event_time = f"{h:02d}:{mi:02d}"
                text = text[m.end():]
                break

    description = text.strip()
    if not description:
        return None

    return {
        "date": event_date.strftime("%Y-%m-%d"),
        "time": event_time,
        "description": description,
        "url": url,
    }

# ── 查詢行程 ──────────────────────────────────────────────────────────
def get_schedules(start_date, end_date, owner=None):
    conn = get_conn()
    c = conn.cursor()
    if owner:
        c.execute("""
            SELECT * FROM schedules
            WHERE event_date BETWEEN ? AND ? AND owner = ?
            ORDER BY event_date, event_time NULLS LAST
        """, (start_date, end_date, owner))
    else:
        c.execute("""
            SELECT * FROM schedules
            WHERE event_date BETWEEN ? AND ?
            ORDER BY event_date, event_time NULLS LAST
        """, (start_date, end_date))
    rows = c.fetchall()
    conn.close()
    return rows

def format_schedule_list(rows, title):
    if not rows:
        return f"📅 {title}\n\n（這段時間沒有行程）"

    groups = {USER_NAME_1: [], USER_NAME_2: [], "共同": []}
    for row in rows:
        owner = row[1]
        category = row[2]
        event_date = row[3]
        d = datetime.strptime(event_date, "%Y-%m-%d").date()
        line = f"{format_date(d)} {format_row_for_list(row)}"
        if category == "共同":
            groups["共同"].append(line)
        elif owner == USER_ID_1:
            groups[USER_NAME_1].append(line)
        elif owner == USER_ID_2:
            groups[USER_NAME_2].append(line)

    lines = [f"📅 {title}\n"]
    for name in [USER_NAME_1, USER_NAME_2]:
        if groups[name]:
            lines.append(name)
            lines.extend(groups[name])
            lines.append("")
    if groups["共同"]:
        lines.append(f"💑 {USER_NAME_1}+{USER_NAME_2}")
        lines.extend(groups["共同"])
        lines.append("")

    return "\n".join(lines).strip()

# ── 衝突檢查 ─────────────────────────────────────────────────────────
def check_conflict(user_id, event_date, event_time):
    if not event_time:
        return None
    conn = get_conn()
    c = conn.cursor()
    other_id = get_other_id(user_id)
    c.execute("""
        SELECT * FROM schedules
        WHERE owner = ? AND event_date = ? AND event_time = ?
    """, (other_id, event_date, event_time))
    row = c.fetchone()
    conn.close()
    return row

def save_schedule(owner_id, category, parsed, created_by):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO schedules
        (owner, category, event_date, event_time, description, url, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (owner_id, category, parsed["date"], parsed["time"],
          parsed["description"], parsed.get("url"), created_by,
          datetime.now().isoformat()))
    conn.commit()
    conn.close()

def do_save(created_by, owner_id, category, parsed, owner_name):
    save_schedule(owner_id, category, parsed, created_by)
    d = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
    time_str = f" {parsed['time']}" if parsed["time"] else ""
    url_str = f"\n🔗 {parsed['url']}" if parsed.get("url") else ""

    if category == "共同":
        confirm = f"✅ 已新增共同行程\n{format_date(d)}{time_str} {parsed['description']}{url_str}"
        creator_name = get_user_name(created_by)
        notify_msg = f"📅 {creator_name}新增了共同行程\n{format_date(d)}{time_str} {parsed['description']}{url_str}"
        other_id = get_other_id(created_by)
        if other_id:
            try:
                line_bot_api.push_message(other_id, TextSendMessage(text=notify_msg))
            except Exception as e:
                print(f"Notify error: {e}")
        return confirm
    else:
        return f"✅ 已新增{owner_name}行程\n{format_date(d)}{time_str} {parsed['description']}{url_str}"

# ── 主訊息處理 ────────────────────────────────────────────────────────
def handle_message(user_id, text):
    text = text.strip()
    today = date.today()

    # ── 狀態機 ────────────────────────────────────────────────────────
    if user_id in user_state:
        state = user_state[user_id]

        # 等待是否共同行程
        if state["step"] == "wait_category":
            data = state["data"]
            if text in ["是", "1"]:
                del user_state[user_id]
                category = "共同"
                if data["parsed"]["time"]:
                    conflict = check_conflict(data["owner_id"], data["parsed"]["date"], data["parsed"]["time"])
                    if conflict:
                        other_name = get_user_name(get_other_id(user_id))
                        user_state[user_id] = {
                            "step": "wait_conflict_confirm",
                            "data": {**data, "category": category}
                        }
                        return (
                            f"⚠️ 注意：{other_name}在 {data['parsed']['date']} {data['parsed']['time']} 已有「{conflict[5]}」\n仍要新增共同行程嗎？",
                            quick_reply_yes_no()
                        )
                return (do_save(user_id, data["owner_id"], "共同", data["parsed"], data["owner_name"]), None)
            elif text in ["否", "2"]:
                del user_state[user_id]
                return (do_save(user_id, data["owner_id"], "個人", data["parsed"], data["owner_name"]), None)
            else:
                del user_state[user_id]
                return ("已取消新增。", None)

        # 等待衝突確認
        if state["step"] == "wait_conflict_confirm":
            data = state["data"]
            del user_state[user_id]
            if text in ["是", "1"]:
                return (do_save(user_id, data["owner_id"], data["category"], data["parsed"], data["owner_name"]), None)
            else:
                return ("已取消新增。", None)

        # 等待刪除選擇
        if state["step"] == "wait_delete":
            rows = state["data"]["rows"]
            if text.isdigit() and 1 <= int(text) <= len(rows):
                row = rows[int(text) - 1]
                conn = get_conn()
                c = conn.cursor()
                c.execute("DELETE FROM schedules WHERE id = ?", (row[0],))
                conn.commit()
                conn.close()
                del user_state[user_id]
                owner_name = get_user_name(row[1])
                d = datetime.strptime(row[3], "%Y-%m-%d").date()
                time_str = f" {row[4]}" if row[4] else ""
                return (f"✅ 已刪除{owner_name}行程\n{format_date(d)}{time_str} {row[5]}", None)
            else:
                del user_state[user_id]
                return ("已取消刪除。", None)

    # ── 查詢指令 ──────────────────────────────────────────────────────
    if any(kw in text for kw in ["明天行程", "查明天"]):
        tomorrow = today + timedelta(days=1)
        rows = get_schedules(tomorrow.strftime("%Y-%m-%d"), tomorrow.strftime("%Y-%m-%d"))
        return (format_schedule_list(rows, f"明天行程（{tomorrow.month}/{tomorrow.day}）"), None)

    if any(kw in text for kw in ["本週行程", "查本週", "這週行程"]):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return (format_schedule_list(rows, f"本週行程（{start.month}/{start.day}～{end.month}/{end.day}）"), None)

    if any(kw in text for kw in ["本月行程", "查本月"]):
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        end = next_month - timedelta(days=1)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return (format_schedule_list(rows, f"本月行程（{start.month}月）"), None)

    if any(kw in text for kw in ["未來行程", "查未來"]):
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31")
        return (format_schedule_list(rows, "未來所有行程"), None)

    # 查自己的行程
    if text == "我的" or f"{USER_NAME_1}行程" in text or f"查{USER_NAME_1}" in text:
        if text == "我的":
            owner_id_query = user_id
            name_query = get_user_name(user_id)
        else:
            owner_id_query = USER_ID_1
            name_query = USER_NAME_1
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=owner_id_query)
        return (format_schedule_list(rows, f"{name_query}的行程"), None)

    # 查對方的行程
    if text in ["他的", "她的"] or f"{USER_NAME_2}行程" in text or f"查{USER_NAME_2}" in text:
        if text in ["他的", "她的"]:
            other_id_query = get_other_id(user_id)
            name_query = get_user_name(other_id_query)
        else:
            other_id_query = USER_ID_2
            name_query = USER_NAME_2
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=other_id_query)
        return (format_schedule_list(rows, f"{name_query}的行程"), None)

    # ── 刪除 ──────────────────────────────────────────────────────────
    if text == "刪除":
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31")
        if not rows:
            return ("目前沒有任何未來行程。", None)
        user_state[user_id] = {"step": "wait_delete", "data": {"rows": rows}}
        lines = ["請選擇要刪除哪筆行程：\n"]
        for i, row in enumerate(rows, 1):
            owner_name = get_user_name(row[1])
            cat_str = "（共同）" if row[2] == "共同" else ""
            d = datetime.strptime(row[3], "%Y-%m-%d").date()
            time_str = f" {row[4]}" if row[4] else ""
            lines.append(f"{i}. {owner_name}｜{format_date(d)}{time_str} {row[5]}{cat_str}")
        lines.append("\n輸入數字選擇，其他輸入取消")
        return ("\n".join(lines), quick_reply_numbers(len(rows)))

    # ── 幫助 ──────────────────────────────────────────────────────────
    if text in ["幫助", "help", "?", "？", "說明", "指令"]:
        return (get_help_text(), None)

    # ── 解析新增行程 ──────────────────────────────────────────────────
    owner_id = user_id
    owner_name = get_user_name(user_id)

    for uid, name in USER_NAMES.items():
        if text.startswith(name + " ") or text.startswith(name + "　"):
            owner_id = uid
            owner_name = name
            text = text[len(name):].strip()
            break

    parsed = parse_schedule(text)
    if parsed:
        if "error" in parsed and parsed["error"] == "past":
            return ("❌ 這個日期已經過去了，請重新輸入。", None)
        user_state[user_id] = {
            "step": "wait_category",
            "data": {"owner_id": owner_id, "owner_name": owner_name, "parsed": parsed}
        }
        return ("這是共同行程嗎？", quick_reply_yes_no())

    return (
        "😅 我看不太懂，試試這樣輸入：\n\n"
        "7/15 11:00 染頭髮\n"
        "明天 下午3點 看牙醫\n"
        f"{USER_NAME_2} 7/20 開會\n"
        "7/15 10:00 繳費 https://xxx.com\n\n"
        "輸入「幫助」查看完整指令",
        None
    )

def get_help_text():
    return (
        "📖 使用說明\n\n"
        "【新增行程】\n"
        "  7/15 11:00 染頭髮\n"
        "  明天 下午3點 看牙醫\n"
        "  後天 買生日禮物\n"
        f"  {USER_NAME_2} 7/20 開會（幫對方新增）\n"
        "  7/15 10:00 繳費 https://xxx.com\n\n"
        "【查詢】\n"
        "  明天行程 / 本週行程\n"
        f"  未來行程\n"
        f"  {USER_NAME_1}行程 / {USER_NAME_2}行程\n\n"
        "【刪除】輸入「刪除」選擇要刪的行程\n\n"
        "【提醒時間】\n"
        "  前一天晚上 11 點\n"
        "  當天早上 9 點"
    )

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
    result = handle_message(user_id, text)
    reply_text, quick_reply = result
    msg = TextSendMessage(text=reply_text, quick_reply=quick_reply)
    line_bot_api.reply_message(event.reply_token, msg)

# ── 提醒排程 ──────────────────────────────────────────────────────────
def reminder_loop():
    while True:
        try:
            send_reminders()
        except Exception as e:
            print(f"Reminder error: {e}")
        time.sleep(600)

def build_reminder_msg(rows, recipient_id):
    if not rows:
        return None
    recipient_name = get_user_name(recipient_id)
    other_id = get_other_id(recipient_id)
    other_name = get_user_name(other_id)
    groups = {recipient_name: [], other_name: [], "共同": []}
    for row in rows:
        owner = row[1]
        category = row[2]
        line = format_row_for_reminder(row)
        if category == "共同":
            groups["共同"].append(line)
        elif owner == recipient_id:
            groups[recipient_name].append(line)
        else:
            groups[other_name].append(line)
    has_content = any(groups[k] for k in groups)
    if not has_content:
        return None
    lines = []
    for name in [recipient_name, other_name]:
        if groups[name]:
            lines.append(name)
            lines.extend(groups[name])
            lines.append("")
    if groups["共同"]:
        lines.append(f"💑 {USER_NAME_1}+{USER_NAME_2}")
        lines.extend(groups["共同"])
        lines.append("")
    return "\n".join(lines).strip()

def send_reminders():
    now = datetime.now()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    conn = get_conn()
    c = conn.cursor()
    if now.hour == 23 and now.minute < 10:
        c.execute("SELECT * FROM schedules WHERE event_date = ? AND reminded_day_before = 0 ORDER BY event_time NULLS LAST", (tomorrow.strftime("%Y-%m-%d"),))
        rows = c.fetchall()
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    try:
                        line_bot_api.push_message(uid, TextSendMessage(text=f"明天行程提醒\n\n{msg}"))
                    except Exception as e:
                        print(f"Push error: {e}")
            for row in rows:
                c.execute("UPDATE schedules SET reminded_day_before = 1 WHERE id = ?", (row[0],))
            conn.commit()
    if now.hour == 9 and now.minute < 10:
        c.execute("SELECT * FROM schedules WHERE event_date = ? AND reminded_same_day = 0 ORDER BY event_time NULLS LAST", (today.strftime("%Y-%m-%d"),))
        rows = c.fetchall()
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    try:
                        line_bot_api.push_message(uid, TextSendMessage(text=f"今天行程提醒\n\n{msg}"))
                    except Exception as e:
                        print(f"Push error: {e}")
            for row in rows:
                c.execute("UPDATE schedules SET reminded_same_day = 1 WHERE id = ?", (row[0],))
            conn.commit()
    conn.close()

# ── 啟動 ──────────────────────────────────────────────────────────────
init_db()
t = threading.Thread(target=reminder_loop, daemon=True)
t.start()

if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
