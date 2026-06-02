import os
import re
import sqlite3
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
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

# ── 對話狀態（記錄使用者目前在哪個流程）────────────────────────────
# state[user_id] = {
#   "step": "wait_category" | "wait_delete" | "wait_conflict_confirm",
#   "data": { ... }
# }
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
    """查詢時用：顯示時間、事項，有網址加🔗"""
    event_time = row[4]
    description = row[5]
    url = row[6]
    time_str = f"{event_time} " if event_time else ""
    url_str = " 🔗" if url else ""
    return f"{time_str}{description}{url_str}"

def format_row_for_reminder(row):
    """提醒時用：顯示時間、事項，有網址顯示完整"""
    event_time = row[4]
    description = row[5]
    url = row[6]
    time_str = f"{event_time} " if event_time else ""
    url_str = f"\n🔗 {url}" if url else ""
    return f"{time_str}{description}{url_str}"

# ── 解析日期時間 ──────────────────────────────────────────────────────
def parse_schedule(text):
    today = date.today()
    year = today.year
    event_date = None
    event_time = None
    url = None

    # 抽出網址
    url_match = re.search(r'https?://\S+', text)
    if url_match:
        url = url_match.group(0)
        text = text[:url_match.start()].strip()

    # 相對日期
    relative = {"今天": 0, "明天": 1, "後天": 2, "大後天": 3}
    for word, delta in relative.items():
        if text.startswith(word):
            event_date = today + timedelta(days=delta)
            text = text[len(word):].strip()
            break

    # 絕對日期
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

    # 過去日期檢查
    if event_date < today:
        return {"error": "past"}

    # 時間
    time_patterns = [
        (r"^(下午|晚上)\s*(\d{1,2})[點::](\d{2})\s*", lambda m: (int(m.group(2)) + 12 if int(m.group(2)) < 12 else int(m.group(2)), int(m.group(3)))),
        (r"^(下午|晚上)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2)) + 12 if int(m.group(2)) < 12 else int(m.group(2)), 0)),
        (r"^(早上|上午|早)\s*(\d{1,2})[點::](\d{2})\s*", lambda m: (int(m.group(2)), int(m.group(3)))),
        (r"^(早上|上午|早)\s*(\d{1,2})\s*點\s*", lambda m: (int(m.group(2)), 0)),
        (r"^(\d{1,2})[::點](\d{2})\s*", lambda m: (int(m.group(1)), int(m.group(2)))),
        (r"^(\d{1,2})\s*點(\d{2})?\s*", lambda m: (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)),
    ]
    for pattern, extractor in time_patterns:
        m = re.match(pattern, text)
        if m:
            h, mi = extractor(m)
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
    """分人顯示，沒行程的人不出現，共同排最後"""
    if not rows:
        return f"📅 {title}\n\n（這段時間沒有行程）"

    # 分組
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
            lines.append(f"{name}")
            lines.extend(groups[name])
            lines.append("")
    if groups["共同"]:
        lines.append(f"💑 阿馨+阿虎")
        lines.extend(groups["共同"])
        lines.append("")

    return "\n".join(lines).strip()

# ── 衝突檢查 ─────────────────────────────────────────────────────────
def check_conflict(owner_id, event_date, event_time):
    """只檢查共同行程跟對方的行程是否衝突"""
    if not event_time:
        return None
    conn = get_conn()
    c = conn.cursor()
    other_id = get_other_id(owner_id)
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

# ── 主訊息處理 ────────────────────────────────────────────────────────
def handle_message(user_id, text):
    text = text.strip()
    today = date.today()

    # ── 狀態機：等待中的流程 ──────────────────────────────────────────
    if user_id in user_state:
        state = user_state[user_id]

        # 等待選分類
        if state["step"] == "wait_category":
            if text in ["1", "2", "3"]:
                cat_map = {"1": "自己", "2": "工作", "3": "共同"}
                category = cat_map[text]
                data = state["data"]
                del user_state[user_id]

                # 共同行程：檢查衝突
                if category == "共同" and data["parsed"]["time"]:
                    conflict = check_conflict(data["owner_id"], data["parsed"]["date"], data["parsed"]["time"])
                    if conflict:
                        other_name = get_user_name(get_other_id(user_id))
                        user_state[user_id] = {
                            "step": "wait_conflict_confirm",
                            "data": {**data, "category": category}
                        }
                        d = datetime.strptime(data["parsed"]["date"], "%Y-%m-%d").date()
                        return (
                            f"⚠️ 注意：{other_name}在 {data['parsed']['date']} {data['parsed']['time']} 已有「{conflict[5]}」\n"
                            f"仍要新增共同行程嗎？\n1. 是\n2. 否"
                        )

                return do_save(user_id, data["owner_id"], category, data["parsed"], data["owner_name"])
            else:
                del user_state[user_id]
                return "已取消新增。"

        # 等待衝突確認
        if state["step"] == "wait_conflict_confirm":
            data = state["data"]
            del user_state[user_id]
            if text == "1":
                return do_save(user_id, data["owner_id"], data["category"], data["parsed"], data["owner_name"])
            else:
                return "已取消新增。"

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
                return f"✅ 已刪除{owner_name}行程\n{format_date(d)}{time_str} {row[5]}"
            else:
                del user_state[user_id]
                return "已取消刪除。"

    # ── 查詢指令 ──────────────────────────────────────────────────────
    if any(kw in text for kw in ["明天行程", "查明天"]):
        tomorrow = today + timedelta(days=1)
        rows = get_schedules(tomorrow.strftime("%Y-%m-%d"), tomorrow.strftime("%Y-%m-%d"))
        return format_schedule_list(rows, f"明天行程（{tomorrow.month}/{tomorrow.day}）")

    if any(kw in text for kw in ["本週行程", "查本週", "這週行程"]):
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return format_schedule_list(rows, f"本週行程（{start.month}/{start.day}～{end.month}/{end.day}）")

    if any(kw in text for kw in ["本月行程", "查本月"]):
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        end = next_month - timedelta(days=1)
        rows = get_schedules(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return format_schedule_list(rows, f"本月行程（{start.month}月）")

    if f"{USER_NAME_1}行程" in text or f"查{USER_NAME_1}" in text:
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=USER_ID_1)
        return format_schedule_list(rows, f"{USER_NAME_1}的行程")

    if f"{USER_NAME_2}行程" in text or f"查{USER_NAME_2}" in text:
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31", owner=USER_ID_2)
        return format_schedule_list(rows, f"{USER_NAME_2}的行程")

    # ── 刪除 ──────────────────────────────────────────────────────────
    if text == "刪除":
        rows = get_schedules(today.strftime("%Y-%m-%d"), "2999-12-31")
        if not rows:
            return "目前沒有任何未來行程。"
        user_state[user_id] = {"step": "wait_delete", "data": {"rows": rows}}
        lines = ["請選擇要刪除哪筆行程：\n"]
        for i, row in enumerate(rows, 1):
            owner_name = get_user_name(row[1])
            cat = row[2]
            d = datetime.strptime(row[3], "%Y-%m-%d").date()
            time_str = f" {row[4]}" if row[4] else ""
            lines.append(f"{i}. {owner_name}｜{format_date(d)}{time_str} {row[5]}（{cat}）")
        lines.append("\n輸入數字選擇，其他輸入取消")
        return "\n".join(lines)

    # ── 幫助 ──────────────────────────────────────────────────────────
    if text in ["幫助", "help", "?", "？", "說明", "指令"]:
        return get_help_text()

    # ── 解析新增行程 ──────────────────────────────────────────────────
    # 檢查是否幫對方新增
    owner_id = user_id
    owner_name = get_user_name(user_id)

    for uid, name in USER_NAMES.items():
        if text.startswith(name + " ") or text.startswith(name + "　"):
            owner_id = uid
            owner_name = name
            text = text[len(name):].strip()
            break

    # 共同行程
    if text.startswith("共同 ") or text.startswith("共同　"):
        text = text[2:].strip()
        parsed = parse_schedule(text)
        if parsed:
            if "error" in parsed and parsed["error"] == "past":
                return "❌ 這個日期已經過去了，請重新輸入。"
            user_state[user_id] = {
                "step": "wait_category",
                "data": {"owner_id": "共同", "owner_name": "共同", "parsed": parsed}
            }
            # 直接存共同
            return do_save(user_id, "共同", "共同", parsed, "共同")

    parsed = parse_schedule(text)
    if parsed:
        if "error" in parsed and parsed["error"] == "past":
            return "❌ 這個日期已經過去了，請重新輸入。"
        user_state[user_id] = {
            "step": "wait_category",
            "data": {"owner_id": owner_id, "owner_name": owner_name, "parsed": parsed}
        }
        return "請選擇分類：\n1. 自己\n2. 工作\n3. 共同"

    return (
        "😅 我看不太懂，試試這樣輸入：\n\n"
        "7/15 11:00 染頭髮\n"
        "明天 下午3點 看牙醫\n"
        "阿虎 7/20 開會\n"
        "共同 7/25 19:00 吃晚餐\n\n"
        "輸入「幫助」查看完整指令"
    )

def do_save(created_by, owner_id, category, parsed, owner_name):
    """實際存入資料庫，並處理共同行程通知"""
    save_schedule(owner_id, category, parsed, created_by)

    d = datetime.strptime(parsed["date"], "%Y-%m-%d").date()
    time_str = f" {parsed['time']}" if parsed["time"] else ""
    url_str = f"\n🔗 {parsed['url']}" if parsed.get("url") else ""

    if category == "共同":
        confirm = f"✅ 已新增共同行程\n{format_date(d)}{time_str} {parsed['description']}{url_str}"
        # 通知對方
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
        display_name = owner_name if owner_id != created_by else get_user_name(created_by)
        return f"✅ 已新增{display_name}行程\n{format_date(d)}{time_str} {parsed['description']}{url_str}"

def get_help_text():
    return (
        "📖 使用說明\n\n"
        "【新增行程】\n"
        "  7/15 11:00 染頭髮\n"
        "  明天 下午3點 看牙醫\n"
        "  後天 買生日禮物\n"
        "  阿虎 7/20 開會（幫對方新增）\n"
        "  共同 7/25 19:00 吃晚餐\n"
        "  7/15 10:00 繳費 https://xxx.com\n\n"
        "【分類】新增後選 1自己／2工作／3共同\n\n"
        "【查詢】\n"
        "  明天行程 / 本週行程 / 本月行程\n"
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
    reply = handle_message(user_id, text)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

# ── 提醒排程 ──────────────────────────────────────────────────────────
def reminder_loop():
    while True:
        try:
            send_reminders()
        except Exception as e:
            print(f"Reminder error: {e}")
        time.sleep(600)

def build_reminder_msg(rows, recipient_id):
    """建立提醒訊息，收到的人自己排最前，對方其次，共同最後"""
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
        lines.append("💑 阿馨+阿虎")
        lines.extend(groups["共同"])
        lines.append("")

    return "\n".join(lines).strip()

def send_reminders():
    now = datetime.now()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    conn = get_conn()
    c = conn.cursor()

    # 前一天晚上 23 點
    if now.hour == 23 and now.minute < 10:
        c.execute("SELECT * FROM schedules WHERE event_date = ? AND reminded_day_before = 0 ORDER BY event_time NULLS LAST", (tomorrow.strftime("%Y-%m-%d"),))
        rows = c.fetchall()
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    full_msg = f"明天行程提醒\n\n{msg}"
                    try:
                        line_bot_api.push_message(uid, TextSendMessage(text=full_msg))
                    except Exception as e:
                        print(f"Push error: {e}")
            for row in rows:
                c.execute("UPDATE schedules SET reminded_day_before = 1 WHERE id = ?", (row[0],))
            conn.commit()

    # 當天早上 9 點
    if now.hour == 9 and now.minute < 10:
        c.execute("SELECT * FROM schedules WHERE event_date = ? AND reminded_same_day = 0 ORDER BY event_time NULLS LAST", (today.strftime("%Y-%m-%d"),))
        rows = c.fetchall()
        if rows:
            for uid in ALL_USER_IDS:
                msg = build_reminder_msg(rows, uid)
                if msg:
                    full_msg = f"今天行程提醒\n\n{msg}"
                    try:
                        line_bot_api.push_message(uid, TextSendMessage(text=full_msg))
                    except Exception as e:
                        print(f"Push error: {e}")
            for row in rows:
                c.execute("UPDATE schedules SET reminded_same_day = 1 WHERE id = ?", (row[0],))
            conn.commit()

    conn.close()

# ── 啟動 ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    t = threading.Thread(target=reminder_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
