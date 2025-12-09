from flask import Flask, render_template, request, redirect, url_for, session
import redis
import time
from datetime import datetime, date, timezone, timedelta
import os
from dotenv import load_dotenv  # ⬅ 讀取 .env

load_dotenv()  # ⬅ 讀取 .env

app = Flask(__name__)

# Flask Secret Key 從環境變數來
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")

# Redis URL 從環境變數來
REDIS_URL = os.getenv("REDIS_URL")
# print(">>> 使用的 REDIS_URL：", REDIS_URL)

if not REDIS_URL:
    raise RuntimeError("環境變數 REDIS_URL 沒有設定，請確認 .env 檔")

# 連線到雲端 Redis
r = redis.from_url(REDIS_URL, decode_responses=True)

# 統一用台灣時間（UTC+8）
TZ = timezone(timedelta(hours=8))


# -----------------------------------------------------
# 工具函式
# -----------------------------------------------------
def calc_rot_info(created_at, deadline_ts, is_routine,
                  initial_rot=0, interval_days=0, last_checkin_ts=None):
    """
    算目前腐爛度 + emoji + 毒雞湯 + 顏色 bucket
    現在只會出現 0 / 30 / 60 / 90 四個等級
    """
    now = time.time()

    # created_at 轉成 timestamp（用台灣時間）
    try:
        created_at = float(created_at)
    except (TypeError, ValueError):
        if isinstance(created_at, str) and "T" in created_at:
            try:
                dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S")
                dt = dt.replace(tzinfo=TZ)
                created_at = dt.timestamp()
            except Exception:
                created_at = now
        else:
            created_at = now

    # last_checkin_ts：沒有就用 created_at
    base_ts = created_at
    if last_checkin_ts:
        try:
            base_ts = float(last_checkin_ts)
        except (TypeError, ValueError):
            base_ts = created_at

    is_routine = str(is_routine) == "1"

    # interval_days
    try:
        interval_days = int(interval_days)
    except (TypeError, ValueError):
        interval_days = 0
    if interval_days <= 0:
        interval_days = 1  # 預設 1 天

    # -------- initial_rot 也強制變成 0 / 30 / 60 / 90 --------
    try:
        initial_rot = int(initial_rot)
    except ValueError:
        initial_rot = 0

    if initial_rot <= 0:
        initial_rot = 0
    elif initial_rot <= 30:
        initial_rot = 30
    elif initial_rot <= 60:
        initial_rot = 60
    else:
        initial_rot = 90

    # ------------------------------------------------
    # 系統推估腐爛度 base_level（也只有 0 / 30 / 60 / 90）
    # ------------------------------------------------
    if is_routine or not deadline_ts:
        # 習慣 / 無期限：看「距離上次打卡（或建立）經過了幾倍間隔」
        delta_days = (now - base_ts) / 86400.0
        ratio = delta_days / interval_days

        if ratio < 0.3:
            base_level = 0
        elif ratio < 1:
            base_level = 30
        elif ratio < 3:
            base_level = 60
        else:
            base_level = 90
    else:
        # 有 deadline 的任務
        try:
            deadline_ts = float(deadline_ts)
        except (TypeError, ValueError):
            if isinstance(deadline_ts, str) and "T" in deadline_ts:
                try:
                    dt = datetime.strptime(deadline_ts, "%Y-%m-%dT%H:%M:%S")
                    dt = dt.replace(tzinfo=TZ)
                    deadline_ts = dt.timestamp()
                except Exception:
                    deadline_ts = now
            else:
                deadline_ts = now

        diff_hours = (now - deadline_ts) / 3600  # 正數 = 已經超過 deadline

        # 這裡也只給四階
        if diff_hours < -48:       # 提前兩天以上
            base_level = 0
        elif diff_hours < 0:       # 截止前 48 小時內
            base_level = 30
        elif diff_hours < 72:      # 截止前後 3 天內
            base_level = 60
        else:                      # 超過 3 天還沒做
            base_level = 90

    # --------- 緩衝機制：剛建立 / 剛修改 6 小時內不會變臭 ---------
    GRACE_HOURS = 6
    age_hours = max(0.0, (now - float(created_at)) / 3600.0)

    if age_hours < GRACE_HOURS:
        # 6 小時內 → 一律用你選的起始腐爛度
        level = initial_rot
    else:
        # 之後才開始看 base_level（系統推估）跟 initial_rot 誰比較高
        level = max(base_level, initial_rot)

    # 安全一下，如果有小數或其他狀況，再壓回四個等級
    if level < 15:
        level = 0
    elif level < 45:
        level = 30
    elif level < 75:
        level = 60
    else:
        level = 90

    # emoji + 毒雞湯 + 顏色 bucket
    if level == 0:
        emoji = "🍀"
        message = "完全新鮮，現在開始剛剛好！"
        bucket = "fresh"
    elif level == 30:
        emoji = "🌱"
        message = "半熟半爛、還救得回來！"
        bucket = "mild"
    elif level == 60:
        emoji = "🍄"
        message = "楞著幹嘛？還不快去做！"
        bucket = "medium"
    else:  # 90
        emoji = "💥"
        message = "腐爛爆表沒救了，就你最會拖！"
        bucket = "critical"

    return {
        "level": level,
        "emoji": emoji,
        "message": message,
        "bucket": bucket,
    }



def format_deadline(deadline_ts):
    """把 deadline 轉成好看的字串，沒有就顯示無期限。"""
    if not deadline_ts:
        return "無期限 / 習慣型任務"
    try:
        ts = float(deadline_ts)
    except (TypeError, ValueError):
        if isinstance(deadline_ts, str) and "T" in deadline_ts:
            try:
                dt = datetime.strptime(deadline_ts, "%Y-%m-%dT%H:%M:%S")
                dt = dt.replace(tzinfo=TZ)
                ts = dt.timestamp()
            except Exception:
                return str(deadline_ts)
        else:
            return str(deadline_ts)
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%Y-%m-%d %H:%M")


def safe_display_time(any_value):
    """把 created_at 可能是秒數或 ISO 字串，轉成 'YYYY-MM-DD HH:MM' 顯示用。"""
    now = time.time()
    if any_value is None or any_value == "":
        ts = now
    else:
        try:
            ts = float(any_value)
        except (TypeError, ValueError):
            if isinstance(any_value, str) and "T" in any_value:
                try:
                    dt = datetime.strptime(any_value, "%Y-%m-%dT%H:%M:%S")
                    dt = dt.replace(tzinfo=TZ)
                    ts = dt.timestamp()
                except Exception:
                    ts = now
            else:
                ts = now
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")


def to_datetime_local(deadline_ts):
    """給 edit 頁面用，把 deadline_ts 轉成 input[type=datetime-local] 的字串。"""
    if not deadline_ts:
        return ""
    try:
        ts = float(deadline_ts)
    except (TypeError, ValueError):
        if isinstance(deadline_ts, str) and "T" in deadline_ts:
            try:
                dt = datetime.strptime(deadline_ts, "%Y-%m-%dT%H:%M:%S")
                dt = dt.replace(tzinfo=TZ)
                ts = dt.timestamp()
            except Exception:
                return ""
        else:
            return ""
    dt = datetime.fromtimestamp(ts, TZ)
    return dt.strftime("%Y-%m-%dT%H:%M")


def is_today(ts_value):
    """判斷 timestamp 是否是今天（給打卡使用），以台灣時間為準"""
    if not ts_value:
        return False
    try:
        ts = float(ts_value)
    except (TypeError, ValueError):
        return False
    d = datetime.fromtimestamp(ts, TZ).date()
    return d == datetime.now(TZ).date()


# -----------------------------------------------------
# 使用者相關小工具
# -----------------------------------------------------
def get_queue_keys(owner_key):
    """
    owner_key 是真正用來區分使用者的 key（名字 + 密語）
    """
    if owner_key:
        return f"today_queue:{owner_key}", f"today_queue:{owner_key}:current"
    # 沒設定時用共用 key（理論上現在不會用到）
    return "today_queue", "today_queue:current"


def get_current_owner():
    """
    回傳 (owner_key, display_name)

    - owner_key：實際寫進 Redis 的 owner，格式像「伶伶#mySecret123」
    - display_name：畫面上顯示的名字（不含密語）
    """
    return session.get("owner_key"), session.get("display_name")


# -----------------------------------------------------
# 登入頁 / 根路徑
# -----------------------------------------------------
@app.route("/")
def root():
    """
    進到根網址先來登入頁，
    如果已經登入就直接跳首頁 /home
    """
    owner_key, display_name = get_current_owner()
    if owner_key:
        return redirect(url_for("index"))
    return redirect(url_for("login"))


# -----------------------------------------------------
# 登出 / 清空登入狀態
# -----------------------------------------------------
@app.route("/logout", methods=["POST"])
def logout():
    # 清掉 session 裡的 owner_key / display_name 等等
    session.clear()
    # 回到登入頁
    return redirect(url_for("login"))


@app.route("/login")
def login():
    """
    顯示登入畫面（輸入名字 + 密語）
    """
    owner_key, display_name = get_current_owner()
    if owner_key:
        return redirect(url_for("index"))
    return render_template("login.html")


# -----------------------------------------------------
# 設定 / 切換使用者（名字 + 密語）
# -----------------------------------------------------
@app.route("/set_owner", methods=["POST"])
def set_owner():
    # 顯示用的名字
    name = request.form.get("owner", "").strip()
    # 像密碼一樣的密語
    secret = request.form.get("secret", "").strip()

    if not name:
        # 如果沒填名字就先用「匿名」，但還是會被唯一限制擋下來
        name = "匿名"

    if not secret:
        # 沒填密語 → 回登入頁，順便帶錯誤訊息
        return render_template(
            "login.html",
            error="請輸入密語（類似密碼）。",
            last_name=name,
        )

    # Redis 裡用 user:<name> 來記「這個名字的密語」
    user_key = f"user:{name}"
    stored_secret = r.get(user_key)

    if stored_secret is None:
        # 第一次使用這個名字 → 註冊並綁定密語
        r.set(user_key, secret)
    else:
        # 名字已存在，但密語不同 → 不允許重複名字
        if stored_secret != secret:
            return render_template(
                "login.html",
                error="這個名字已經被使用，密語不正確。請輸入正確密語或換一個名字。",
                last_name=name,
            )

    # 真正用來分資料的 key：名字 + 密語（保持原本格式，舊資料不會壞掉）
    owner_key = f"{name}#{secret}"

    # 寫入 session
    session["owner_key"] = owner_key      # 後端 / Redis 用
    session["display_name"] = name        # 前端顯示用

    # 登入成功 → 去首頁 /home
    return redirect(url_for("index"))


# -----------------------------------------------------
# 首頁（登入後）
# -----------------------------------------------------
@app.route("/home")
def index():
    owner_key, display_name = get_current_owner()

    # 如果還沒登入，一律丟回登入頁
    if not owner_key:
        return redirect(url_for("login"))

    # 讀出所有任務 ID（所有人共用 list，等等用 owner_key 過濾）
    task_ids = r.lrange("tasks", 0, -1)

    tasks = []
    tasks_by_id = {}

    category_mapping = {
        "作業": "homework",
        "考試": "exam",
        "生活": "life",
        "習慣": "habit",
        "其他": "other",
    }
    categories = ["homework", "exam", "life", "habit", "other"]

    for tid in task_ids:
        key = f"task:{tid}"
        data = r.hgetall(key)
        if not data:
            continue

        task_owner = data.get("owner")
        # 只顯示屬於目前登入者的任務
        if task_owner != owner_key:
            continue

        # 正規化分類（舊資料如果是中文，改成英文代碼）
        raw_cat = data.get("category", "other")
        cat = raw_cat
        if raw_cat in category_mapping:
            cat = category_mapping[raw_cat]
            if cat != raw_cat:
                r.hset(key, "category", cat)

        interval_days = int(data.get("interval_days", 0) or 0)
        last_checkin_ts = data.get("last_checkin_ts")

        initial_rot = data.get("initial_rot", 0)

        rot_info = calc_rot_info(
            data.get("created_at", time.time()),
            data.get("deadline_ts", ""),
            data.get("is_routine", "0"),
            initial_rot,
            interval_days,
            last_checkin_ts,
        )

        task_obj = {
            "id": tid,
            "title": data.get("title", ""),
            "category": cat,
            "created_at": safe_display_time(data.get("created_at")),
            "deadline_str": format_deadline(data.get("deadline_ts", "")),
            "is_routine": data.get("is_routine", "0") == "1",
            "initial_rot": int(initial_rot) if initial_rot else 0,
            "rot_level": rot_info["level"],
            "rot_emoji": rot_info["emoji"],
            "rot_message": rot_info["message"],
            "rot_bucket": rot_info["bucket"],
            "interval_days": interval_days,
            "checked_today": is_today(last_checkin_ts),
        }

        tasks.append(task_obj)
        tasks_by_id[tid] = task_obj

    # 依照腐爛程度排序（越臭越前面）
    tasks.sort(key=lambda t: t["rot_level"], reverse=True)

    # -----------------------------------------------------
    # 自動重建分類索引（Set Index）→ 跟 owner_key 綁在一起
    # -----------------------------------------------------
    for c in categories:
        r.delete(f"idx:{owner_key}:cat:{c}")

    for t in tasks:
        cat = t["category"]
        if cat not in categories:
            cat = "other"
        r.sadd(f"idx:{owner_key}:cat:{cat}", t["id"])

    category_counts = {
        c: r.scard(f"idx:{owner_key}:cat:{c}") for c in categories
    }
    total_tasks = len(tasks)

    # -----------------------------------------------------
    # Sorted Set：最臭任務排行榜（每個 owner_key 一份）
    # -----------------------------------------------------
    rot_rank_key = f"rot_rank:{owner_key}"
    pipe = r.pipeline(transaction=False)
    pipe.delete(rot_rank_key)
    for tid, t in tasks_by_id.items():
        pipe.zadd(rot_rank_key, {tid: t["rot_level"]})
    pipe.execute()

    top_rot_tasks = []
    top_raw = r.zrevrange(rot_rank_key, 0, 2, withscores=True)
    for tid, score in top_raw:
        t = tasks_by_id.get(tid)
        if t:
            top_rot_tasks.append({
                "id": tid,
                "title": t["title"],
                "rot_level": int(score),
                "category": t["category"],
            })

    # -----------------------------------------------------
    # Streams：最近操作紀錄（含打卡）→ 只看自己的 owner_key
    # -----------------------------------------------------
    events_raw = r.xrevrange("task_events", max="+", min="-", count=100)
    events = []
    for ev_id, fields in events_raw:
        if fields.get("owner") != owner_key:
            continue

        ev_type = fields.get("type", "")
        title = fields.get("title")
        task_id = fields.get("task_id")
        ts_val = fields.get("ts")

        if ts_val:
            try:
                ts = float(ts_val)
                dt = datetime.fromtimestamp(ts, TZ)
                time_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                time_str = ""
        else:
            time_str = ""

        base = title or (f"任務 #{task_id}" if task_id else "(未知)")

        if ev_type == "created":
            action = "新增"
        elif ev_type == "deleted":
            action = "刪除"
        elif ev_type == "queue_add":
            action = "加入今日救援"
        elif ev_type == "rescue_pick":
            action = "抽中救援任務"
        elif ev_type == "updated":
            action = "修改"
        elif ev_type == "checkin":
            action = "打卡"
        else:
            action = "操作"

        events.append({
            "id": ev_id,
            "text": f"{action}：{base}",
            "time_str": time_str,
        })

    # -----------------------------------------------------
    # 完成任務紀錄（另一條 Streams）→ 只看自己的 owner_key
    # -----------------------------------------------------
    done_raw = r.xrevrange("task_done", max="+", min="-", count=50)
    done_events = []
    for ev_id, fields in done_raw:
        if fields.get("owner") != owner_key:
            continue

        title = fields.get("title")
        task_id = fields.get("task_id")
        ts_val = fields.get("ts")

        if ts_val:
            try:
                ts = float(ts_val)
                dt = datetime.fromtimestamp(ts, TZ)
                time_str = dt.strftime("%m-%d %H:%M")
            except Exception:
                time_str = ""
        else:
            time_str = ""

        base = title or (f"任務 #{task_id}" if task_id else "(未知)")
        done_events.append({
            "id": ev_id,
            "text": f"完成：{base}",
            "time_str": time_str,
        })

    # -----------------------------------------------------
    # 今日救援 Queue 狀態（每個 owner_key 自己一個 queue）
    # -----------------------------------------------------
    queue_key, current_key = get_queue_keys(owner_key)
    queue_count = r.llen(queue_key)

    rescue_task = None
    current_id = r.get(current_key)
    if current_id:
        key = f"task:{current_id}"
        data = r.hgetall(key)
        if data and data.get("owner") == owner_key:
            interval_days = int(data.get("interval_days", 0) or 0)
            last_checkin_ts = data.get("last_checkin_ts")
            initial_rot = data.get("initial_rot", 0)
            rot_info = calc_rot_info(
                data.get("created_at", time.time()),
                data.get("deadline_ts", ""),
                data.get("is_routine", "0"),
                initial_rot,
                interval_days,
                last_checkin_ts,
            )
            rescue_task = {
                "id": current_id,
                "title": data.get("title", ""),
                "category": data.get("category", ""),
                "created_at": safe_display_time(data.get("created_at")),
                "deadline_str": format_deadline(data.get("deadline_ts", "")),
                "is_routine": data.get("is_routine", "0") == "1",
                "initial_rot": int(initial_rot) if initial_rot else 0,
                "rot_level": rot_info["level"],
                "rot_emoji": rot_info["emoji"],
                "rot_message": rot_info["message"],
                "rot_bucket": rot_info["bucket"],
                "interval_days": interval_days,
                "checked_today": is_today(last_checkin_ts),
            }

    return render_template(
        "index.html",
        tasks=tasks,
        rescue_task=rescue_task,
        queue_count=queue_count,
        top_rot_tasks=top_rot_tasks,
        category_counts=category_counts,
        total_tasks=total_tasks,
        events=events,
        done_events=done_events,
        owner=display_name,
    )


# -----------------------------------------------------
# 新增任務
# -----------------------------------------------------
@app.route("/add", methods=["POST"])
def add_task():
    owner_key, display_name = get_current_owner()
    if not owner_key:
        # 沒登入就不讓新增
        return redirect(url_for("index"))

    title = request.form.get("title", "").strip()
    category = request.form.get("category", "other")
    deadline_str = request.form.get("deadline", "").strip()
    no_deadline = request.form.get("no_deadline")

    initial_rot_str = request.form.get("initial_rot", "0")
    try:
        initial_rot = int(initial_rot_str)
    except ValueError:
        initial_rot = 0

    interval_str = request.form.get("interval_days", "").strip()
    try:
        interval_days = int(interval_str)
    except ValueError:
        interval_days = 0

    created_at = time.time()
    is_routine = 0
    deadline_ts = ""

    if no_deadline == "on" or not deadline_str:
        is_routine = 1
        deadline_ts = ""
        if interval_days <= 0:
            interval_days = 1
    else:
        try:
            dt = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
            dt = dt.replace(tzinfo=TZ)
            deadline_ts = dt.timestamp()
        except ValueError:
            deadline_ts = ""
            is_routine = 1
            if interval_days <= 0:
                interval_days = 1

    if not title:
        return redirect(url_for("index"))

    new_id = r.incr("task:id")
    new_id_str = str(new_id)
    key = f"task:{new_id_str}"

    pipe = r.pipeline(transaction=True)
    pipe.hset(key, mapping={
        "id": new_id_str,
        "title": title,
        "category": category,
        "created_at": created_at,
        "deadline_ts": deadline_ts,
        "is_routine": is_routine,
        "initial_rot": initial_rot,
        "interval_days": interval_days,
        "last_checkin_ts": "",
        "owner": owner_key,
    })
    pipe.rpush("tasks", new_id_str)
    pipe.sadd(f"idx:{owner_key}:cat:{category}", new_id_str)
    pipe.execute()

    r.xadd("task_events", {
        "type": "created",
        "task_id": new_id_str,
        "title": title,
        "category": category,
        "owner": owner_key,
        "ts": str(int(created_at)),
    })

    return redirect(url_for("index"))


# -----------------------------------------------------
# 編輯任務
# -----------------------------------------------------
@app.route("/edit/<task_id>", methods=["GET", "POST"])
def edit_task(task_id):
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "other")
        deadline_str = request.form.get("deadline", "").strip()
        no_deadline = request.form.get("no_deadline")

        initial_rot_str = request.form.get("initial_rot", "0")
        try:
            initial_rot = int(initial_rot_str)
        except ValueError:
            initial_rot = 0

        interval_str = request.form.get("interval_days", "").strip()
        try:
            interval_days = int(interval_str)
        except ValueError:
            interval_days = 0

        old_category = data.get("category", "other")
        created_at = data.get("created_at", time.time())
        last_checkin_ts = data.get("last_checkin_ts", "")
        is_routine = 0
        deadline_ts = ""

        if no_deadline == "on" or not deadline_str:
            is_routine = 1
            deadline_ts = ""
            if interval_days <= 0:
                interval_days = 1
        else:
            try:
                dt = datetime.strptime(deadline_str, "%Y-%m-%dT%H:%M")
                dt = dt.replace(tzinfo=TZ)
                deadline_ts = dt.timestamp()
            except ValueError:
                deadline_ts = ""
                is_routine = 1
                if interval_days <= 0:
                    interval_days = 1

        if not title:
            return redirect(url_for("index"))

        pipe = r.pipeline(transaction=True)
        pipe.hset(key, mapping={
            "title": title,
            "category": category,
            "created_at": created_at,
            "deadline_ts": deadline_ts,
            "is_routine": is_routine,
            "initial_rot": initial_rot,
            "interval_days": interval_days,
            "last_checkin_ts": last_checkin_ts,
            "owner": owner_key,
        })

        if old_category != category:
            pipe.srem(f"idx:{owner_key}:cat:{old_category}", task_id)
            pipe.sadd(f"idx:{owner_key}:cat:{category}", task_id)

        pipe.execute()

        r.xadd("task_events", {
            "type": "updated",
            "task_id": task_id,
            "title": title,
            "category": category,
            "owner": owner_key,
            "ts": str(int(time.time())),
        })

        return redirect(url_for("index"))

    task = {
        "id": task_id,
        "title": data.get("title", ""),
        "category": data.get("category", "other"),
        "initial_rot": int(data.get("initial_rot", 0) or 0),
        "is_routine": data.get("is_routine", "0") == "1",
        "interval_days": int(data.get("interval_days", 0) or 0),
    }
    deadline_input = to_datetime_local(data.get("deadline_ts", ""))

    return render_template(
        "edit.html",
        task=task,
        deadline_input=deadline_input,
    )


# -----------------------------------------------------
# 打卡
# -----------------------------------------------------
@app.route("/checkin/<task_id>", methods=["GET", "POST"])
def checkin_task(task_id):
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    if request.method == "POST":
        note = request.form.get("note", "").strip()
        now_ts = time.time()

        r.hset(key, "last_checkin_ts", now_ts)

        title = data.get("title", "")
        r.xadd("task_checkin", {
            "task_id": task_id,
            "title": title,
            "note": note,
            "owner": owner_key,
            "ts": str(int(now_ts)),
        })
        r.xadd("task_events", {
            "type": "checkin",
            "task_id": task_id,
            "title": title,
            "owner": owner_key,
            "ts": str(int(now_ts)),
        })

        return redirect(url_for("index"))

    last_ts = data.get("last_checkin_ts")
    last_str = ""
    if last_ts:
        try:
            ts = float(last_ts)
            last_str = datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")
        except Exception:
            last_str = ""

    task = {
        "id": task_id,
        "title": data.get("title", ""),
    }

    return render_template(
        "checkin.html",
        task=task,
        last_checkin=last_str,
    )


# -----------------------------------------------------
# 檢視打卡紀錄（全部）→ 只看自己的 owner_key
# -----------------------------------------------------
@app.route("/checkins")
def view_checkins():
    """
    檢視所有打卡紀錄（從 Redis Stream: task_checkin 抓最近 100 筆）
    """
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    events_raw = r.xrevrange("task_checkin", max="+", min="-", count=100)

    records = []
    for ev_id, fields in events_raw:
        if fields.get("owner") != owner_key:
            continue

        title = fields.get("title", "")
        note = fields.get("note", "")
        task_id = fields.get("task_id", "")
        ts_val = fields.get("ts")

        time_str = ""
        if ts_val:
            try:
                ts = float(ts_val)
                dt = datetime.fromtimestamp(ts, TZ)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_str = ""

        records.append({
            "title": title or (f"任務 #{task_id}" if task_id else "(未知任務)"),
            "note": note,
            "time_str": time_str,
            "task_id": task_id,
        })

    return render_template("checkins.html", records=records)


# -----------------------------------------------------
# 完成任務（移出清單 + 記入完成紀錄）
# -----------------------------------------------------
@app.route("/done/<task_id>", methods=["POST"])
def done_task(task_id):
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    title = data.get("title", "") if data else ""
    category = data.get("category", "other") if data else "other"

    now_ts = time.time()
    r.xadd("task_done", {
        "task_id": task_id,
        "title": title,
        "category": category,
        "owner": owner_key,
        "ts": str(int(now_ts)),
    })

    # 接著就像刪除一樣，把它從清單移除
    if data:
        r.delete(key)
    r.lrem("tasks", 0, task_id)
    r.srem(f"idx:{owner_key}:cat:{category}", task_id)
    r.zrem(f"rot_rank:{owner_key}", task_id)

    queue_key, current_key = get_queue_keys(owner_key)
    r.lrem(queue_key, 0, task_id)
    current_id = r.get(current_key)
    if current_id == task_id:
        r.delete(current_key)

    return redirect(url_for("index"))


# -----------------------------------------------------
# 刪除（真的不要做了）
# -----------------------------------------------------
@app.route("/delete/<task_id>", methods=["POST"])
def delete_task(task_id):
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    category = data.get("category", "other")
    title = data.get("title", "")

    r.delete(key)
    r.lrem("tasks", 0, task_id)
    r.srem(f"idx:{owner_key}:cat:{category}", task_id)
    r.zrem(f"rot_rank:{owner_key}", task_id)

    queue_key, current_key = get_queue_keys(owner_key)
    r.lrem(queue_key, 0, task_id)
    current_id = r.get(current_key)
    if current_id == task_id:
        r.delete(current_key)

    r.xadd("task_events", {
        "type": "deleted",
        "task_id": task_id,
        "title": title,
        "owner": owner_key,
        "ts": str(int(time.time())),
    })

    return redirect(url_for("index"))


# -----------------------------------------------------
# Queue：加入今日救援 & 抽下個
# -----------------------------------------------------
@app.route("/queue/add/<task_id>", methods=["POST"])
def add_to_queue(task_id):
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    queue_key, _ = get_queue_keys(owner_key)
    current_list = r.lrange(queue_key, 0, -1)
    if task_id not in current_list:
        r.rpush(queue_key, task_id)
        title = data.get("title", "")
        r.xadd("task_events", {
            "type": "queue_add",
            "task_id": task_id,
            "title": title or "",
            "owner": owner_key,
            "ts": str(int(time.time())),
        })

    return redirect(url_for("index"))


@app.route("/queue/next", methods=["POST"])
def next_rescue():
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    queue_key, current_key = get_queue_keys(owner_key)
    tid = r.lpop(queue_key)
    if tid:
        r.set(current_key, tid)
        key = f"task:{tid}"
        data = r.hgetall(key)
        title = data.get("title") if data else ""
        r.xadd("task_events", {
            "type": "rescue_pick",
            "task_id": tid,
            "title": title or "",
            "owner": owner_key,
            "ts": str(int(time.time())),
        })
    else:
        r.delete(current_key)
    return redirect(url_for("index"))


# -----------------------------------------------------
# 檢視「單一任務」的打卡紀錄
# -----------------------------------------------------
@app.route("/checkins/<task_id>")
def view_task_checkins_by_task(task_id):
    """只看某一個任務的打卡紀錄"""
    owner_key, display_name = get_current_owner()
    if not owner_key:
        return redirect(url_for("index"))

    key = f"task:{task_id}"
    data = r.hgetall(key)
    if not data or data.get("owner") != owner_key:
        return redirect(url_for("index"))

    title = data.get("title", f"任務 #{task_id}") if data else f"任務 #{task_id}"

    events_raw = r.xrevrange("task_checkin", max="+", min="-", count=200)
    records = []
    for ev_id, fields in events_raw:
        if fields.get("owner") != owner_key:
            continue
        if fields.get("task_id") != str(task_id):
            continue

        note = fields.get("note", "")
        ts_val = fields.get("ts")
        time_str = ""
        if ts_val:
            try:
                ts = float(ts_val)
                dt = datetime.fromtimestamp(ts, TZ)
                time_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_str = ""

        records.append({
            "note": note,
            "time_str": time_str,
        })

    return render_template(
        "task_checkins.html",
        task_title=title,
        task_id=task_id,
        records=records,
    )


if __name__ == "__main__":
    # 這樣手機在同一個 Wi-Fi 下，用 http://你的IP:5000 就能連進來
    app.run(host="0.0.0.0", port=5000, debug=True)
