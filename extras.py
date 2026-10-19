"""
額外原始數據（只記錄，唔分析）：
  - 更易事項、天氣及跑道狀況：賽馬日 10:50 同頭場開跑前約 60 分鐘各讀一次
  - 賽後第二日 10:00 後重讀賽果同派彩，補返漏咗嘅行
  - 補錄過往賽果同派彩（data/state/backfill.json 入面嘅待補清單）
  - 每場「開跑前10」記錄完之後，通知 Claude 排程（如有設定 ROUTINE_FIRE_URL / ROUTINE_FIRE_TOKEN）
全部出錯都只會記錄，唔會影響賠率記錄。
"""
import json
import os
import re
from datetime import datetime, timedelta

import recorder as R

CHANGES_URL = "https://racing.hkjc.com/zh-hk/local/info/changes"
WEATHER_URL = "https://racing.hkjc.com/zh-hk/local/info/windtracker"
CHANGE_COLS = ["記錄時間HKT", "日期", "場地", "時間點", "場次", "更易內容"]
WEATHER_COLS = ["記錄時間HKT", "日期", "場地", "時間點", "賽事資料", "跑道", "場地狀況", "度地儀指數", "氣溫",
                "相對濕度", "總雨量", "土壤濕度", "最後更新"]
BACKFILL = R.STATE_DIR / "backfill.json"
BACKFILL_MINUTES = 20


def _rc():
    import racecard
    return racecard


def _page_text(site, url):
    site.page.goto(url, wait_until="domcontentloaded")
    site.page.wait_for_timeout(5000)
    return site.page.evaluate("() => document.body.innerText")


# ---------- 更易事項、天氣及跑道狀況 ----------
def parse_changes(text):
    i = text.find("更易事項")
    if i < 0:
        return None
    body = text[i + 4:]
    end = body.find("賽事資料")
    body = body[:end] if end > 0 else body
    out = []
    for m in re.finditer(r"第\s*(\d+)\s*場\s*(.*?)(?=第\s*\d+\s*場|$)", body, re.S):
        for line in [x.strip() for x in m.group(2).split("\n")]:
            if line and line != "--":
                out.append((int(m.group(1)), line))
    return out


def parse_weather(text, d):
    if f"{d:%d/%m/%Y}" not in text:
        return None
    g = lambda p: (re.search(p, text) or [None, ""])[1].strip()
    base = {"賽事資料": g(r"((?:日賽|夜賽)[^\n]*)"), "氣溫": g(r"氣溫\s*\n+\s*([\d.]+)"),
            "相對濕度": g(r"相對濕度\s*\n+\s*([\d.]+)"), "總雨量": g(r"總雨量\s*\n+\s*([\d.]+)"),
            "土壤濕度": g(r"土壤濕度\s*\n+\s*([\d.]+)"), "最後更新": g(r"最後更新:\s*([^\n]+)")}
    rows = []
    for m in re.finditer(r"\n\s*(草地|全天候跑道)\s*\n+\s*場地\s*([^\n]*)\n+\s*度地儀指數\s*\n+\s*([^\n]+)", text):
        rows.append(base | {"跑道": m.group(1), "場地狀況": m.group(2).strip(), "度地儀指數": m.group(3).strip()})
    return rows or [base | {"跑道": "", "場地狀況": "", "度地儀指數": ""}]


def snapshot_info(site, d, st, tag):
    """讀更易事項同天氣及跑道狀況，各寫入一個 CSV。更易事項只記新出現嘅內容。"""
    done = st.setdefault("info_done", [])
    if tag in done:
        return
    rc = _rc()
    ds, venue, ts = st["date"], st["venue"], rc.stamp()
    try:
        wtext = _page_text(site, WEATHER_URL)
        w = parse_weather(wtext, d)
        if w is None:
            R.log_issue("天氣場地", f"{ds} {tag}", "天氣及跑道狀況頁唔係今日賽期")
        else:
            if venue not in (w[0]["賽事資料"] or venue):
                R.log_issue("天氣場地", f"{ds} {tag}", f"頁面賽事資料同場地唔對：{w[0]['賽事資料']}")
            rc.append_csv(rc.season_file(d, "天氣場地"), WEATHER_COLS,
                          [{"記錄時間HKT": ts, "日期": ds, "場地": venue, "時間點": tag, **r} for r in w])
        ctext = _page_text(site, CHANGES_URL)
        ch = parse_changes(ctext)
        if ch is None:
            R.log_issue("更易事項", f"{ds} {tag}", "更易事項頁讀唔到內容")
        else:
            p = rc.season_file(d, "更易事項")
            seen = {(r["場次"], r["更易內容"]) for r in rc.read_csv(p) if r["日期"] == ds}
            new = [{"記錄時間HKT": ts, "日期": ds, "場地": venue, "時間點": tag, "場次": n, "更易內容": c}
                   for n, c in ch if (str(n), c) not in seen]
            rc.append_csv(p, CHANGE_COLS, new)
            print(f"  更易事項 {tag}：{len(ch)} 項，新增 {len(new)} 項")
        R.push_now(f"更易事項及場地 {tag}")
    except Exception as e:  # noqa: BLE001
        R.log_issue("更易事項/天氣場地", f"{ds} {tag}", f"讀取出錯：{e!r}"[:200])
    finally:
        done.append(tag)  # 每個時間點只試一次
        R.save_state(d, st)


def info_due(st, d, now):
    """回傳今次要做嘅更易事項時間點：10:50、頭場前60。"""
    done = st.get("info_done", [])
    if "10:50" not in done and R.hm(now) >= R.hm(datetime(2000, 1, 1, *R.EARLY_FROM)):
        return "10:50"
    if "頭場前60" not in done and st.get("post"):
        first = min(st["post"].values())
        post = datetime.combine(d, datetime.strptime(first, "%H:%M").time(), tzinfo=R.HKT)
        if post - timedelta(minutes=62.5) <= now < post:
            return "頭場前60"
    return None


# ---------- 賽後第二日重讀 ----------
def recheck_target(now):
    """尋日或者前日係賽馬日、已記錄賽果、未重讀，而而家係 10:00 後：回傳嗰日。"""
    if R.hm(now) < 10 * 60:
        return None
    for back in (1, 2):
        d = now.date() - timedelta(days=back)
        st = R.load_state(d)
        if st and st.get("race_day") and st.get("results_done") and not st.get("recheck_done"):
            return d
    return None


def recheck(d):
    """只試一次：出錯都當做咗，費事每次執行都重試。"""
    st = R.load_state(d)
    st["recheck_done"] = True
    R.save_state(d, st)
    _recheck(d, dict(st))


def _recheck(d, st):
    rc = _rc()
    ds, venue, code = st["date"], st["venue"], st["code"]
    p_res, p_div = rc.season_file(d, "賽果"), rc.season_file(d, "派彩")
    have_res = {(r["場次"], r["馬號"], r["馬名"]) for r in rc.read_csv(p_res) if r["日期"] == ds}
    have_div = {(r["場次"], r["彩池"], r["勝出組合"]) for r in rc.read_csv(p_div) if r["日期"] == ds}
    add_res, add_div = [], []
    for n in st["races"]:
        try:
            html = rc.fetch(rc.results_url(d, code, n), "名次")
        except Exception as e:  # noqa: BLE001
            R.log_issue("賽果/派彩", f"{ds} 第{n}場", f"第二日重讀讀唔到：{e!r}"[:200])
            continue
        info, rows = rc.parse_results(html, n, d, venue)
        divs = rc.parse_dividends(html)
        ts = rc.stamp()
        for r in rows or []:
            if (str(n), r["馬號"], r["馬名"]) not in have_res:
                add_res.append({"記錄時間HKT": ts, **{k: info[k] for k in rc.RACE_COLS}, **r})
        for x in divs:
            if (str(n), x["彩池"], x["勝出組合"]) not in have_div:
                add_div.append({"記錄時間HKT": ts, "日期": ds, "場地": venue, "場次": n, **x})
    rc.append_csv(p_res, rc.RESULT_COLS, add_res)
    rc.append_csv(p_div, rc.DIV_COLS, add_div)
    if add_res or add_div:
        R.log_issue("賽果/派彩", ds, f"第二日重讀補返 賽果 {len(add_res)} 行、派彩 {len(add_div)} 行")
    R.push_now(f"重讀賽果 {ds}")
    print(f"重讀 {ds} 賽果：補返賽果 {len(add_res)} 行、派彩 {len(add_div)} 行")


# ---------- 補錄過往賽果 ----------
def backfill_pending():
    if not BACKFILL.exists():
        return []
    return json.loads(BACKFILL.read_text(encoding="utf-8")).get("pending", [])


def backfill():
    """逐個補錄 pending 入面嘅 'YYYY/MM/DD:ST' 賽馬日嘅賽果同派彩（已有嘅場次會略過）。"""
    rc = _rc()
    data = json.loads(BACKFILL.read_text(encoding="utf-8"))
    start = R.now_hkt()
    while data.get("pending") and R.now_hkt() < start + timedelta(minutes=BACKFILL_MINUTES):
        item = data["pending"].pop(0)  # 先移走，出錯都唔會無限重試
        BACKFILL.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        ds, code = item.split(":")
        d = datetime.strptime(ds, "%Y/%m/%d").date()
        print(f"補錄 {ds} {code} 賽果…")
        for n in range(1, 15):
            have = {r["場次"] for r in rc.read_csv(rc.season_file(d, "賽果")) if r["日期"] == ds}
            if str(n) in have:
                continue
            try:
                rc.do_results(d, code, [n])
            except Exception as e:  # noqa: BLE001
                print(f"  第 {n} 場讀唔到，當呢日完結：{e!r}"[:200])
                break
            if str(n) not in {r["場次"] for r in rc.read_csv(rc.season_file(d, "賽果")) if r["日期"] == ds}:
                break  # 冇呢場（已經過咗尾場）
        got = {r["場次"] for r in rc.read_csv(rc.season_file(d, "賽果")) if r["日期"] == ds}
        if not got:
            R.log_issue("賽果", ds, f"補錄讀唔到任何賽果（{code}）")
        data.setdefault("done", []).append(f"{item}:{len(got)}場")
        BACKFILL.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        R.push_now(f"補錄賽果 {ds}")


def idle_work_due(now):
    return bool(backfill_pending()) or recheck_target(now) is not None


def idle_work(now):
    """唔使記錄賠率嗰陣做嘅雜項。出錯只記錄。"""
    try:
        d = recheck_target(now)
        if d:
            recheck(d)
    except Exception as e:  # noqa: BLE001
        R.log_issue("賽果/派彩", "第二日重讀", f"出錯：{e!r}"[:200])
    try:
        if backfill_pending():
            backfill()
    except Exception as e:  # noqa: BLE001
        R.log_issue("賽果", "補錄", f"出錯：{e!r}"[:200])
    finally:
        try:
            _rc().close()
        except Exception:  # noqa: BLE001
            pass


# ---------- 通知 Claude 排程 ----------
def fire_routine(st, races):
    url_raw, tok_raw = os.environ.get("ROUTINE_FIRE_URL", ""), os.environ.get("ROUTINE_FIRE_TOKEN", "")
    # Secret 有時會連成段 curl 指令一齊貼咗入去：只抽出網址同 token 本身
    m = re.search(r"https://\S+?/fire", url_raw) or re.search(r"https://\S+?/fire", tok_raw)
    url = m.group(0) if m else url_raw.strip()
    m = re.search(r"sk-ant-[A-Za-z0-9_\-]+", tok_raw)
    token = m.group(0) if m else tok_raw.strip()
    if not url or not token or not races:
        if races:
            R.log_issue("Claude排程", st["date"], "冇設定通知網址或者 token")
        return
    import requests
    for race in races:
        text = f"{st['date']} {st['venue']}（{st['code']}）第 {race} 場 開跑前10 賠率已經記錄。"
        try:
            r = requests.post(url, timeout=30, json={"text": text}, headers={
                "Authorization": f"Bearer {token}", "anthropic-beta": "experimental-cc-routine-2026-04-01",
                "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
            print(f"  已通知 Claude 排程：第 {race} 場（HTTP {r.status_code}）")
            if r.status_code >= 300:
                R.log_issue("Claude排程", f"{st['date']} 第{race}場", f"通知失敗 HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            R.log_issue("Claude排程", f"{st['date']} 第{race}場", f"通知出錯：{type(e).__name__}")  # 唔記錄內容，費事露出 token
