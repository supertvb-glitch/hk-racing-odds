"""
香港賽馬會（HKJC）獨贏 / 位置賠率記錄器
只讀取公開賠率頁，唔會登入、唔會投注。

用法：
  python recorder.py auto     # 由 GitHub Actions 定時呼叫，按香港時間自動揀做乜
  python recorder.py early    # 強制記錄早段賠率
  python recorder.py prerace  # 檢查並記錄開跑前 60/30/10 分鐘賠率（同埋賽後走勢）
  python recorder.py trend    # 強制記錄賽後走勢
  python recorder.py probe    # 測試能唔能夠讀到 bet.hkjc.com（唔寫任何資料）
  python recorder.py need     # 唔開瀏覽器，判斷今次需唔需要開瀏覽器（俾 workflow 用）
"""
import csv
import json
from time import sleep
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
BASE = "https://bet.hkjc.com/ch/racing"
DATA = Path(__file__).parent / "data"
STATE_DIR = DATA / "state"
VENUES = {"ST": "沙田", "HV": "跑馬地"}
CODES = {v: k for k, v in VENUES.items()}

COLUMNS = ["記錄時間HKT", "日期", "場地", "場次", "開跑時間", "距開跑分鐘", "時間點類別",
           "馬號", "馬名", "檔位", "負磅", "騎師", "練馬師", "獨贏賠率", "位置賠率",
           "獨贏彩池投注額", "位置彩池投注額"]

# 開跑前時間點：(類別, 目標分鐘)。距開跑 <= 目標+2.5 分鐘，而且仲未過咗下一個時間點，就記錄一次。
TARGETS = [("開跑前60", 60), ("開跑前30", 30), ("開跑前10", 10)]
EARLY_FROM, EARLY_UNTIL = (10, 40), (11, 30)   # 早段：10:40 – 11:29
PRERACE_FROM = (11, 30)                         # 開跑前：11:30 起
TREND_DELAY_MIN = 15                            # 最後一場開跑後 15 分鐘讀走勢
TREND_DEADLINE = (23, 55)                       # 午夜前一定要完成
STAY_MINUTES = 300                              # GitHub 定時執行經常漏咗，所以賽馬日每次執行會留低最多 5 個鐘，自己等下一個時間點；之後再自動觸發下一次執行
OVERNIGHT_HOURS = (22, 23)                      # 隔夜賠率：前一晚 22:00 試，未開賣 23:00 再試

# 喺頁面入面執行嘅 JavaScript：讀出賽期、場次資料同賠率表（獨贏/位置頁同走勢頁共用）
EXTRACT_JS = r"""
() => {
  const T = s => (s || '').replace(/\s+/g, ' ').trim();
  const leaves = [...document.querySelectorAll('div,span,h3')].filter(e => e.children.length === 0);
  const find = re => { const e = leaves.find(x => re.test(T(x.textContent))); return e ? T(e.textContent) : null; };
  const meet = find(/^\d\d\/\d\d\/\d{4}, 星期., \S+$/);
  const upd = find(/^更新時間/);
  const infoEl = document.querySelector('.meeting-info-content-text');
  const nums = [...document.querySelectorAll('.raceNoSlider .race-no-item')].map(e => T(e.innerText)).filter(t => /^\d+$/.test(t)).map(Number);
  const activeEl = document.querySelector('.raceNoSlider .race-no-item.active');
  const tb = document.querySelector('table.rc-odds-table');
  let times = [], rows = [];
  if (tb) {
    const trs = [...tb.querySelectorAll('tr')];
    if (trs[1] && !trs[1].classList.contains('rc-odds-row')) times = [...trs[1].children].map(c => T(c.innerText));
    rows = [...tb.querySelectorAll('tr.rc-odds-row')].map(r => ({cls: T(r.className), cells: [...r.children].map(c => T(c.innerText))}));
  }
  return {url: location.href, meet, upd, info: infoEl ? T(infoEl.innerText) : null,
          races: [...new Set(nums)].sort((a, b) => a - b), active: activeEl ? T(activeEl.innerText) : null,
          hasTable: !!tb, times, rows};
}
"""


# ---------- 工具 ----------
def now_hkt():
    return datetime.now(HKT)


def season_name(d):
    y = d.year if d.month >= 9 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}季"


def csv_path(d):
    return DATA / f"{season_name(d)}_賠率記錄.csv"


def state_path(d):
    return STATE_DIR / f"{d:%Y-%m-%d}.json"


def load_state(d):
    p = state_path(d)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_state(d, st):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(d).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def odds_str(s):
    """賠率只接受數字；退出馬或者冇賠率就留空。"""
    return s if num(s) is not None else ""


def clean_jockey(s):
    return re.sub(r"\s*\(-?\d+\)\s*$", "", s or "").strip()


def existing_keys(d):
    p = csv_path(d)
    keys = set()
    if p.exists():
        with open(p, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                keys.add((r["日期"], r["場次"], r["時間點類別"]))
    return keys


def append_rows(d, rows):
    """寫入 CSV，回傳寫入行數。"""
    if not rows:
        return 0
    p = csv_path(d)
    DATA.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"  寫入 {len(rows)} 行 -> {p.name}")
    return len(rows)


LOG_COLS = ["記錄時間", "檔案", "場次", "問題"]


def log_issue(file, scope, problem, ok=False):
    """數據質素問題寫入 data/記錄日誌.csv（ok=True 只 print，唔寫）。"""
    print(f"  [{'OK' if ok else '問題'}] {file} {scope}：{problem}")
    if ok:
        return
    p = DATA / "記錄日誌.csv"
    DATA.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        w.writerow({"記錄時間": now_hkt().strftime("%Y/%m/%d %H:%M:%S"), "檔案": file, "場次": scope, "問題": problem})


def check_rows(rows, scope, expect=None):
    """檢查一場嘅馬匹數目同有冇漏賠率。"""
    horses = [r for r in rows if r.get("馬號")]
    live = [r for r in horses if r.get("檔位")]
    miss = [r["馬號"] for r in live if not r.get("獨贏賠率") or not r.get("位置賠率")]
    if not horses:
        log_issue("賠率記錄", scope, "冇任何馬匹")
    if miss:
        log_issue("賠率記錄", scope, f"漏賠率：{', '.join(miss)} 號")
    if expect and len(horses) != expect:
        log_issue("賠率記錄", scope, f"馬匹數目 {len(horses)} 同排位表 {expect} 唔同")


GIT_ID = ["-c", "user.name=github-actions[bot]",
          "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]


def chain_next():
    """喺 GitHub Actions 入面觸發下一次執行（賽馬日未做完先用）。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    r = subprocess.run(["gh", "workflow", "run", "record-odds.yml", "-f", "mode=auto"],
                       cwd=Path(__file__).parent, capture_output=True, text=True)
    print("已觸發下一次執行" if r.returncode == 0 else f"  ⚠️ 觸發下一次執行失敗：{(r.stderr or r.stdout).strip()[:300]}")


def push_now(msg):
    """喺 GitHub Actions 入面即刻 commit 同 push 新資料（本地執行唔會 push）。失敗只會警告，唔會停。"""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    root = DATA  # data/ 係私人數據 repo（hk-racing-data）嘅 clone
    if not (root / ".git").exists():
        print("  ⚠️ data/ 唔係 git repo，唔 push")
        return

    def git(*args):
        return subprocess.run(["git", *GIT_ID, *args], cwd=root, capture_output=True, text=True)

    try:
        git("add", "-A", ".")
        if git("diff", "--cached", "--quiet").returncode == 0:
            return  # 冇改動
        stamp = now_hkt().strftime("%Y/%m/%d %H:%M")
        r = git("commit", "-q", "-m", f"賠率記錄 {stamp} HKT {msg}")
        if r.returncode != 0:
            print(f"  ⚠️ commit 失敗：{(r.stderr or r.stdout).strip()[:300]}")
            return
        for attempt in range(3):
            pull = git("pull", "--rebase", "--autostash", "-q")
            push = git("push", "-q") if pull.returncode == 0 else pull
            if push.returncode == 0:
                print(f"  已即時 push：{msg}")
                return
            print(f"  ⚠️ push 第 {attempt + 1} 次失敗：{(push.stderr or push.stdout).strip()[:300]}")
            sleep(5)
        print("  ⚠️ 即時 push 三次都失敗，資料會留待 workflow 最後一步再 push")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 即時 push 出錯（唔影響記錄）：{e!r}"[:300])


# ---------- 讀網頁 ----------
CURRENT_SITE = None


class Site:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=True)
        self.page = self.browser.new_page(locale="zh-HK", timezone_id="Asia/Hong_Kong")
        self.page.set_default_timeout(45000)
        global CURRENT_SITE
        CURRENT_SITE = self  # racecard.fetch 後備時共用呢個瀏覽器，唔再開第二個


    def close(self):
        global CURRENT_SITE
        if CURRENT_SITE is self:
            CURRENT_SITE = None
        self.browser.close()
        self._pw.stop()

    def read(self, kind, d, code, race):
        url = f"{BASE}/{kind}/{d:%Y-%m-%d}/{code}/{race}"
        last = None
        for attempt in range(3):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                self.page.wait_for_selector("table.rc-odds-table tr.rc-odds-row", timeout=45000)
                self.page.wait_for_timeout(1500)  # 等賠率數字填好
                data = self.page.evaluate(EXTRACT_JS)
                if data["rows"]:
                    return data
                last = "表格冇資料"
            except Exception as e:  # noqa: BLE001
                last = repr(e)[:300]
            print(f"  第 {attempt + 1} 次讀取失敗：{url} {last}")
            self.page.wait_for_timeout(5000)
        raise RuntimeError(f"讀唔到 {url}：{last}")

    def turnover(self, d, code, race):
        """讀「投注額」頁嘅獨贏／位置彩池投注額。格式未驗證，搵唔到就留空，唔會估。"""
        url = f"{BASE}/turnover/{d:%Y-%m-%d}/{code}/{race}"
        try:
            self.page.goto(url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(4000)
            text = self.page.evaluate("() => document.body.innerText")
        except Exception as e:  # noqa: BLE001
            print(f"  投注額頁讀唔到（唔影響賠率記錄）：{e!r}"[:200])
            return "", ""
        d_txt = f"{d:%d/%m/%Y}"
        if d_txt not in text:
            return "", ""
        def grab(name):
            m = re.search(name + r"[^\n$\d]{0,12}\$\s*([\d,]{4,})", text)
            return m.group(1).replace(",", "") if m else ""
        return grab("獨贏"), grab("位置")


def parse_meet(meet):
    """'11/10/2026, 星期日, 沙田' -> (date, 場地名)"""
    m = re.match(r"(\d\d)/(\d\d)/(\d{4}), 星期., (\S+)", meet or "")
    if not m:
        return None, None
    return datetime(int(m[3]), int(m[2]), int(m[1])).date(), m[4]


def parse_post(info, d):
    """'11/10 (日), 13:30, 第四班, ...' -> 開跑 datetime（香港時間）"""
    m = re.match(r"(\d\d)/(\d\d) \(.\), (\d\d):(\d\d)", info or "")
    if not m:
        return None
    return datetime(d.year, int(m[2]), int(m[1]), int(m[3]), int(m[4]), tzinfo=HKT)


def detect_meeting(site, d):
    """今日（香港日期）有冇本地賽事。回傳 state dict。"""
    for code in ("ST", "HV"):
        data = site.read("wp", d, code, 1)
        md, venue = parse_meet(data["meet"])
        print(f"  {code}: 頁面顯示賽期 {data['meet']!r}")
        if md == d and venue in CODES:
            return {"race_day": True, "date": f"{d:%Y/%m/%d}", "code": CODES[venue], "venue": venue,
                    "races": data["races"], "post": {}, "trend_done": False}
    return {"race_day": False, "date": f"{d:%Y/%m/%d}"}


def ensure_state(site, d):
    st = load_state(d)
    if st is None:
        print("判斷今日有冇本地賽事…")
        st = detect_meeting(site, d)
        save_state(d, st)
    return refresh_races(site, d, st)


def refresh_races(site, d, st):
    """預早偵測到嘅賽事可能未有場次清單，有需要就重新讀一次。"""
    if st.get("race_day") and not st.get("races"):
        print("賽事場次清單係空，重新讀取…")
        new = detect_meeting(site, d)
        if new.get("race_day") and new.get("races"):
            st = st | {"races": new["races"], "code": new["code"], "venue": new["venue"]}
            save_state(d, st)
    return st


def horse_rows(data, st, race, post, rec_time, category, win_idx=7, place_idx=8, mins=None):
    out = []
    for r in data["rows"]:
        c = r["cells"]
        if not c or not re.fullmatch(r"\d+", c[0] or ""):
            continue  # 略過「全餐」等非馬匹行
        m = mins if mins is not None else round((post - rec_time).total_seconds() / 60)
        if "rc-row-scratch" in r["cls"] or len(c) <= max(win_idx, place_idx or 0):
            # 退出馬：頁面只顯示「退出」，冇檔位/騎練/賠率，照記但留空
            out.append({k: "" for k in COLUMNS} | {
                "記錄時間HKT": rec_time.strftime("%Y/%m/%d %H:%M:%S"), "日期": st["date"], "場地": st["venue"],
                "場次": race, "開跑時間": post.strftime("%H:%M"), "距開跑分鐘": m, "時間點類別": category,
                "馬號": c[0], "馬名": c[2] if len(c) > 2 else ""})
            continue
        out.append({
            "記錄時間HKT": rec_time.strftime("%Y/%m/%d %H:%M:%S"), "日期": st["date"], "場地": st["venue"],
            "場次": race, "開跑時間": post.strftime("%H:%M"), "距開跑分鐘": m, "時間點類別": category,
            "馬號": c[0], "馬名": c[2], "檔位": c[3], "負磅": c[4], "騎師": clean_jockey(c[5]), "練馬師": c[6],
            "獨贏賠率": odds_str(c[win_idx]), "位置賠率": odds_str(c[place_idx]) if place_idx is not None else "",
        })
    return out


def add_turnover(site, d, st, race, rows, tag):
    """喺 rows 加入投注額；每日第一次會存一份投注額頁文字樣本，用嚟核對格式。"""
    w, p = site.turnover(d, st["code"], race)
    for r in rows:
        r["獨贏彩池投注額"], r["位置彩池投注額"] = w, p
    return rows


def read_wp(site, d, st, race):
    data = site.read("wp", d, st["code"], race)
    md, _ = parse_meet(data["meet"])
    if md != d:
        raise RuntimeError(f"第 {race} 場頁面日期唔係今日：{data['meet']}")
    if data["active"] and data["active"] != str(race):
        raise RuntimeError(f"要求第 {race} 場，但頁面顯示第 {data['active']} 場")
    post = parse_post(data["info"], d)
    if post is None:
        raise RuntimeError(f"第 {race} 場讀唔到開跑時間：{data['info']!r}")
    st["post"][str(race)] = post.strftime("%H:%M")
    return data, post


# ---------- 三種記錄 ----------
def do_early(site, d, st):
    keys = existing_keys(d)
    rows = []
    for race in st["races"]:
        if (st["date"], str(race), "早段") in keys:
            continue
        data, post = read_wp(site, d, st, race)
        hr = horse_rows(data, st, race, post, now_hkt(), "早段")
        check_rows(hr, f"{st['date']} 第{race}場 早段")
        rows += add_turnover(site, d, st, race, hr, "早段")
        print(f"  第 {race} 場 早段 ✓（開跑 {post:%H:%M}）")
    if append_rows(d, rows):
        push_now("早段")


def due_categories(st, race, now, keys):
    """回傳今次要記錄嘅開跑前類別。"""
    hm = st["post"].get(str(race))
    if not hm:
        return ["?"]  # 未知開跑時間，要先讀頁面
    post = datetime.combine(now.date(), datetime.strptime(hm, "%H:%M").time(), tzinfo=HKT)
    mins = (post - now).total_seconds() / 60
    due = []
    for i, (cat, t) in enumerate(TARGETS):
        nxt = TARGETS[i + 1][1] + 2.5 if i + 1 < len(TARGETS) else 0
        if (st["date"], str(race), cat) not in keys and nxt < mins <= t + 2.5:
            due.append(cat)
    return due[-1:]  # 同一次只記最接近嗰個


def do_prerace(site, d, st):
    now = now_hkt()
    keys = existing_keys(d)
    rows, done = [], []
    for race in st["races"]:
        due = due_categories(st, race, now, keys)
        if not due:
            continue
        data, post = read_wp(site, d, st, race)
        rec = now_hkt()
        mins = (post - rec).total_seconds() / 60
        for cat in due_categories(st, race, rec, keys) if due == ["?"] else due:
            # 用頁面最新開跑時間再確認一次（開跑時間可能改咗）
            t = dict(TARGETS)[cat]
            if mins <= t + 2.5:
                hr = horse_rows(data, st, race, post, rec, cat)
                check_rows(hr, f"{st['date']} 第{race}場 {cat}")
                rows += add_turnover(site, d, st, race, hr, cat)
                keys.add((st["date"], str(race), cat))
                done.append(f"第{race}場{cat}")
                print(f"  第 {race} 場 {cat} ✓（距開跑 {mins:.1f} 分鐘）")
    if append_rows(d, rows):
        push_now("、".join(done))
    return done


def overnight_log():
    p = STATE_DIR / "overnight.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_overnight_log(log):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "overnight.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")


def overnight_due(now):
    """前一晚 21:30／22:30／23:30 各試一次記錄聽日嘅隔夜賠率。"""
    if now.hour not in OVERNIGHT_HOURS:
        return False
    tmr = f"{now.date() + timedelta(days=1):%Y-%m-%d}"
    e = overnight_log().get(tmr, {})
    return not e.get("done") and now.hour not in e.get("tried", [])


def do_overnight(site, now):
    tmr = now.date() + timedelta(days=1)
    key = f"{tmr:%Y-%m-%d}"
    log = overnight_log()
    e = log.setdefault(key, {"tried": []})
    e["tried"] = sorted(set(e["tried"]) | {now.hour})
    try:
        st = load_state(tmr) or detect_meeting(site, tmr)
        st = refresh_races(site, tmr, st)
        if st.get("race_day") and not st.get("races"):
            print(f"聽日（{tmr:%Y/%m/%d}）未有場次清單，遲啲再試")
            return
        if not st.get("race_day"):
            print(f"聽日（{tmr:%Y/%m/%d}）未見本地賽事或者未開賣")
            return
        if load_state(tmr) is None:
            save_state(tmr, st)
        keys = existing_keys(tmr)
        rows = []
        for race in st["races"]:
            if (st["date"], str(race), "隔夜") in keys:
                continue
            data, post = read_wp(site, tmr, st, race)
            hr = horse_rows(data, st, race, post, now_hkt(), "隔夜")
            if not any(num(r["獨贏賠率"]) for r in hr):
                print(f"  第 {race} 場 隔夜賠率未開，遲啲再試")
                save_state(tmr, st)
                return
            check_rows(hr, f"{st['date']} 第{race}場 隔夜")
            rows += add_turnover(site, tmr, st, race, hr, "隔夜")
            print(f"  第 {race} 場 隔夜 ✓")
        n = append_rows(tmr, rows)
        save_state(tmr, st)
        e["done"] = True
        if n:
            push_now(f"隔夜 {tmr:%m/%d}")
    finally:
        save_overnight_log(log)


def next_event(st, d, now):
    """下一個要記錄嘅時間（開跑前時間點窗口開始，或者走勢時間）。"""
    keys = existing_keys(d)
    times = []
    for race in st["races"]:
        hmv = st["post"].get(str(race))
        if not hmv:
            continue
        post = datetime.combine(d, datetime.strptime(hmv, "%H:%M").time(), tzinfo=HKT)
        for cat, t in TARGETS:
            if (st["date"], str(race), cat) not in keys:
                times.append(post - timedelta(minutes=t + 2.5))
    if st["post"] and not st.get("trend_done"):
        last = max(datetime.combine(d, datetime.strptime(v, "%H:%M").time(), tzinfo=HKT) for v in st["post"].values())
        times.append(last + timedelta(minutes=TREND_DELAY_MIN))
    if st.get("trend_done") and not st.get("results_done"):
        times.append(now + timedelta(minutes=5))
    fut = [x for x in times if x > now]
    return min(fut) if fut else None


def do_trend(site, d, st):
    keys = existing_keys(d)
    rows = []
    for race in st["races"]:
        if (st["date"], str(race), "走勢") in keys:
            continue
        data = site.read("pwin", d, st["code"], race)
        md, _ = parse_meet(data["meet"])
        if md != d:
            raise RuntimeError(f"走勢頁已經唔係今日賽期（{data['meet']}），可能已過午夜")
        post = parse_post(data["info"], d)
        times = [t for t in data["times"] if re.fullmatch(r"\d\d:\d\d", t)]
        if not times:
            raise RuntimeError(f"第 {race} 場走勢頁讀唔到時間欄：{data['times']}")
        n = len(times)
        for i, hm in enumerate(times):
            tp = datetime.combine(d, datetime.strptime(hm, "%H:%M").time(), tzinfo=HKT)
            mins = round((post - tp).total_seconds() / 60)
            # 走勢頁：第 7 欄起係各時間點獨贏，最後一欄係位置（只屬最後時間點）
            place_idx = 7 + n if i == n - 1 else None
            for r in horse_rows(data, st, race, post, tp, "走勢", win_idx=7 + i,
                                place_idx=place_idx if place_idx is not None else 7 + i, mins=mins):
                if place_idx is None:
                    r["位置賠率"] = ""
                rows.append(r)
        print(f"  第 {race} 場 走勢 ✓（{', '.join(times)}）")
    n = append_rows(d, rows)
    st["trend_done"] = True
    if n:
        save_state(d, st)
        push_now("走勢")


# ---------- 主程式 ----------
def hm(t):
    return t.hour * 60 + t.minute


def all_prerace_done(st, d, now):
    keys = existing_keys(d)
    for race in st["races"]:
        hmv = st["post"].get(str(race))
        if not hmv:
            return False
        post = datetime.combine(d, datetime.strptime(hmv, "%H:%M").time(), tzinfo=HKT)
        if now < post and any((st["date"], str(race), c) not in keys for c, _ in TARGETS):
            return False
    return True


def trend_due(st, d, now):
    if not st["post"] or len(st["post"]) < len(st["races"]):
        return False
    last = max(datetime.combine(d, datetime.strptime(v, "%H:%M").time(), tzinfo=HKT) for v in st["post"].values())
    return now >= last + timedelta(minutes=TREND_DELAY_MIN)


def need_browser(mode, now):
    d = now.date()
    st = load_state(d)
    t = hm(now)
    if mode in ("probe", "early", "trend", "prerace", "overnight"):
        return True
    if mode == "auto" and not (st and st.get("race_day") and not st.get("results_done")):
        import extras
        if extras.idle_work_due(now):
            return True  # 補錄賽果／第二日重讀
    if overnight_due(now):
        return True
    if t < hm(datetime(2000, 1, 1, *EARLY_FROM)):
        return False
    if st is not None and (not st.get("race_day") or st.get("results_done")) and not st.get("eve_checked"):
        return True  # 每日檢查一次聽日有冇賽事
    if st is None:
        return True
    if not st.get("race_day"):
        return False
    if t < hm(datetime(2000, 1, 1, *EARLY_UNTIL)):
        return (st["date"], str(st["races"][0]), "早段") not in existing_keys(d)
    if st.get("trend_done"):
        return not st.get("results_done") and t < hm(datetime(2000, 1, 1, *TREND_DEADLINE))
    if not st.get("card_done"):
        return True
    keys = existing_keys(d)
    if any(due_categories(st, race, now, keys) for race in st["races"]):
        return True
    if trend_due(st, d, now):
        return True
    nxt = next_event(st, d, now)
    return nxt is not None and nxt <= now + timedelta(minutes=STAY_MINUTES)  # 留低等下一個時間點


def at(d, h, m):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=HKT)


def plan_wake(site, now):
    """加上：賽後第二日 10:00 重讀賽果；補錄未完就即刻再行。"""
    import extras
    cands = [_plan_wake(site, now)]
    st = load_state(now.date())
    if st and st.get("race_day") and st.get("results_done") and not st.get("recheck_done"):
        cands.append(at(now.date() + timedelta(days=1), 10, 0))
    if extras.backfill_pending() and not (st and st.get("race_day") and not st.get("results_done")):
        cands.append(now + timedelta(minutes=1))
    cands = [c for c in cands if c is not None]
    return min(cands) if cands else None


def _plan_wake(site, now):
    """決定下一次要喺幾時執行（香港時間）；唔使就回傳 None。
    賽馬日未完：即刻接力。聽日係賽馬日：等到今晚 22:00／23:00（隔夜）或者聽日 10:50（早段）。"""
    d = now.date()
    st = load_state(d)
    t = hm(now)
    if st and st.get("race_day") and not st.get("results_done"):
        if t < hm(datetime(2000, 1, 1, *EARLY_FROM)):
            return at(d, 10, 50)
        if t < hm(datetime(2000, 1, 1, *TREND_DEADLINE)):
            return now
    tmr = d + timedelta(days=1)
    tst = load_state(tmr)
    if tst is None and site is None:
        tst = {}  # 冇瀏覽器就唔檢查，只睇已存嘅狀態
    if tst is None:
        print(f"檢查聽日（{tmr:%Y/%m/%d}）有冇本地賽事…")
        tst = detect_meeting(site, tmr)
        if tst.get("race_day"):
            save_state(tmr, tst)
        if st is not None:
            st["eve_checked"] = True
            save_state(d, st)
        push_now("檢查聽日賽事")
    if not tst.get("race_day"):
        # 再睇後日：後日有賽事就一直接力，確保前一晚嘅隔夜同當日早段唔會漏
        d2 = d + timedelta(days=2)
        t2 = load_state(d2)
        if t2 is None and site is not None:
            print(f"檢查後日（{d2:%Y/%m/%d}）有冇本地賽事…")
            t2 = detect_meeting(site, d2)
            if t2.get("race_day"):
                save_state(d2, t2)
                push_now("檢查後日賽事")
        if t2 and t2.get("race_day"):
            print(f"後日有賽事：{t2['venue']}，會一直接力")
            return at(tmr, 21, 0)
        print("聽日同後日都冇本地賽事，唔使接力。")
        return None
    print(f"聽日有賽事：{tst['venue']}，會接力等候")
    e = overnight_log().get(f"{tmr:%Y-%m-%d}", {})
    cands = []
    if not e.get("done"):
        cands += [at(d, h, 0) for h in OVERNIGHT_HOURS if at(d, h, 0) > now and h not in e.get("tried", [])]
    cands.append(at(tmr, 10, 50))
    return min(cands)


def keep_alive(wake, started):
    """等到 wake（最多等到今次執行開始後 STAY_MINUTES），然後觸發下一次執行。"""
    limit = started + timedelta(minutes=STAY_MINUTES)
    now = now_hkt()
    target = min(wake, limit)
    if target > now:
        print(f"接力：等到 {target:%m/%d %H:%M}（目標 {wake:%m/%d %H:%M}）再觸發下一次執行…")
        sleep((target - now).total_seconds())
    push_now("狀態")
    chain_next()


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    now = now_hkt()
    d = now.date()
    print(f"香港時間 {now:%Y/%m/%d %H:%M:%S}，模式 {mode}")

    if mode == "need":
        need = need_browser("auto", now)
        print(f"需要開瀏覽器：{need}")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"need={'true' if need else 'false'}\n")
        return

    if mode == "auto" and not need_browser("auto", now):
        print("今次唔使開瀏覽器。")
        wake = plan_wake(None, now)
        if wake is not None:
            keep_alive(wake, now)
        return

    started = now_hkt()
    wake = None
    site = Site()
    try:
        if mode == "probe":
            data = site.read("wp", d, "ST", 1)
            print("成功讀到 bet.hkjc.com")
            print(f"  網址：{data['url']}\n  賽期：{data['meet']}\n  場次資料：{data['info']}")
            print(f"  {data['upd']}\n  場數：{data['races']}\n  馬匹行數：{len(data['rows'])}")
            for r in data["rows"][:3]:
                print("  ", r["cells"][:9])
            return

        if mode == "overnight" or (mode == "auto" and overnight_due(now)):
            do_overnight(site, now)
            if mode == "overnight":
                return

        st = ensure_state(site, d)
        if not st.get("race_day"):
            print("今日（香港時間）冇本地賽事，結束。")
            return
        print(f"今日賽事：{st['venue']}（{st['code']}），共 {len(st['races'])} 場")

        t = hm(now)
        if mode == "early" or (mode == "auto" and hm(datetime(2000, 1, 1, *EARLY_FROM)) <= t < hm(datetime(2000, 1, 1, *EARLY_UNTIL))):
            do_early(site, d, st)
        if mode == "auto":
            import extras
            tag = extras.info_due(st, d, now_hkt())
            if tag == "10:50":
                extras.snapshot_info(site, d, st, tag)
        if mode == "auto" and t >= hm(datetime(2000, 1, 1, *EARLY_FROM)) and not st.get("card_done"):
            import racecard
            racecard.do_card(d, st["code"])
            st = (load_state(d) or st) | {"card_done": True, "post": st["post"], "races": st["races"]}
            save_state(d, st)
            push_now("排位表完成")
        if mode == "prerace" or (mode == "auto" and t >= hm(datetime(2000, 1, 1, *EARLY_FROM))):
            start = now_hkt()
            import extras
            while True:
                done = do_prerace(site, d, st)
                if mode == "auto":
                    tag = extras.info_due(st, d, now_hkt())
                    if tag:
                        extras.snapshot_info(site, d, st, tag)
                    extras.fire_routine(st, [x[1:x.index("場")] for x in done if x.endswith("開跑前10")])
                if not st.get("trend_done") and trend_due(st, d, now_hkt()):
                    if hm(now_hkt()) >= hm(datetime(2000, 1, 1, *TREND_DEADLINE)):
                        raise RuntimeError("已經太接近午夜，未能記錄走勢")
                    do_trend(site, d, st)
                if mode == "auto" and st.get("trend_done") and not st.get("results_done"):
                    import racecard
                    if racecard.do_results(d, st["code"], st["races"]):
                        st["results_done"] = True
                save_state(d, st)
                if mode != "auto" or st.get("results_done") or hm(now_hkt()) >= hm(datetime(2000, 1, 1, *TREND_DEADLINE)):
                    break
                nxt = next_event(st, d, now_hkt())
                limit = start + timedelta(minutes=STAY_MINUTES)
                if nxt is None or nxt > limit:
                    break
                wait = max(5, (nxt - now_hkt()).total_seconds() + 5)
                print(f"等 {wait / 60:.1f} 分鐘到 {nxt:%H:%M:%S} 再記錄…")
                sleep(wait)
        if mode == "trend":
            do_trend(site, d, st)
        if st["races"]:
            d1 = min(st["post"].values()) if st["post"] else None
            if d1:
                st["session"] = "日馬" if d1 < "16:00" else "夜馬"
        save_state(d, st)
    finally:
        failed = sys.exc_info()[0] is not None
        if mode == "auto" and not failed:
            cur = load_state(d)
            if not (cur and cur.get("race_day") and not cur.get("results_done")):
                import extras
                extras.idle_work(now_hkt())
            try:
                wake = plan_wake(site, now_hkt())
            except Exception as e:  # noqa: BLE001
                print(f"  ⚠️ 判斷下一次執行時間出錯：{e!r}"[:300])
                wake = None
        site.close()
        if mode == "auto" and wake is not None and not failed:
            keep_alive(wake, started)
        if mode == "auto" and failed:
            sleep(180)
            chain_next()  # 出錯都接力，等下一次執行再試


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"錯誤：{e}")
        sys.exit(1)
