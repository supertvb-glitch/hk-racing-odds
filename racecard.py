"""
排位表、馬匹往績、賽果同派彩：只記錄原始資料，唔計任何標記或規則。
只讀取 racing.hkjc.com 公開頁面，唔會登入、唔會投注。每次讀網頁之間停 1–2 秒。

用法：
  python racecard.py auto                       # GitHub Actions 定時呼叫
  python racecard.py need                       # 唔讀網頁，判斷今次使唔使做嘢
  python racecard.py card YYYY/MM/DD HV         # 記錄某日排位表同往績
  python racecard.py results YYYY/MM/DD HV      # 記錄某日賽果同派彩
"""
import csv
import os
import random
import re
import sys
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import recorder as R

BASE = "https://racing.hkjc.com/zh-hk/local/information"
UA = "hk-racing-odds data collection script"

RACE_COLS = ["日期", "場地", "場次", "賽事名稱", "開跑時間", "跑道", "賽道", "路程", "場地狀況", "班次", "評分範圍", "獎金"]
CARD_HORSE_COLS = ["馬匹編號", "6次近績", "馬名", "烙號", "負磅", "騎師", "可能超磅", "檔位", "練馬師", "國際評分", "評分",
                   "評分+/-", "排位體重", "排位體重+/-", "最佳時間", "馬齡", "分齡讓磅", "性別", "今季獎金", "優先參賽次序",
                   "上賽距今日數", "配備", "馬主", "父系", "母系", "進口類別"]
CARD_COLS = ["記錄時間HKT"] + RACE_COLS + ["馬匹ID"] + CARD_HORSE_COLS
HIST_HORSE_COLS = ["場次", "名次", "日期", "馬場/跑道/賽道", "途程", "場地狀況", "賽事班次", "檔位", "評分", "練馬師", "騎師",
                   "頭馬距離", "獨贏賠率", "實際負磅", "沿途走位", "完成時間", "排位體重", "配備"]
HIST_RENAME = {"場次": "往績場次編號", "名次": "往績名次", "日期": "往績日期", "場地狀況": "往績場地狀況",
               "檔位": "往績檔位", "評分": "往績評分"}
HIST_COLS = ["記錄時間HKT", "馬匹ID", "馬名"] + [HIST_RENAME.get(c, c) for c in HIST_HORSE_COLS]
RESULT_HORSE_COLS = ["名次", "馬號", "馬名", "騎師", "練馬師", "實際負磅", "排位體重", "檔位", "頭馬距離", "沿途走位",
                     "完成時間", "獨贏賠率"]
RESULT_COLS = ["記錄時間HKT"] + RACE_COLS + RESULT_HORSE_COLS
DIV_COLS = ["記錄時間HKT", "日期", "場地", "場次", "彩池", "勝出組合", "派彩", "單位"]

_session = requests.Session()
_session.headers["User-Agent"] = UA
_pw = None


def T(s):
    return re.sub(r"\s+", " ", s or "").strip()


def stamp():
    return R.now_hkt().strftime("%Y/%m/%d %H:%M:%S")


def fetch(url, marker):
    """先用 requests；HTML 入面搵唔到 marker 先用 Playwright。每次之前停 1–2 秒。唔保存網頁原檔。"""
    global _pw
    time.sleep(random.uniform(1.0, 2.0))
    html = ""
    for attempt in range(3):
        try:
            r = _session.get(url, timeout=30)
            if r.status_code == 200:
                html = r.text
                break
            print(f"  HTTP {r.status_code}：{url}")
        except requests.RequestException as e:
            print(f"  requests 失敗（第 {attempt + 1} 次）：{e!r}"[:300])
        time.sleep(5)
    if marker in html:
        return html
    print(f"  requests 讀到嘅頁面冇「{marker}」，改用 Playwright：{url}")
    site = R.CURRENT_SITE
    if site is None:
        if _pw is None:
            _pw = R.Site()
        site = _pw
    time.sleep(random.uniform(1.0, 2.0))
    site.page.goto(url, wait_until="networkidle")
    html = site.page.content()
    if marker not in html:
        raise RuntimeError(f"讀唔到 {url}（頁面冇「{marker}」）")
    return html


def close():
    global _pw
    if _pw is not None:
        _pw.close()
        _pw = None


def find_table(soup, must):
    for tb in soup.find_all("table"):
        if tb.find("table"):
            continue  # 只要最內層嘅表格
        rows = tb.find_all("tr")
        for i, tr in enumerate(rows[:3]):
            head = [T(c.get_text()) for c in tr.find_all(["th", "td"], recursive=False)]
            if all(any(m == h for h in head) for m in must):
                return head, rows[i + 1:]
    return None, []


def cells(tr):
    return [T(c.get_text()) for c in tr.find_all(["td", "th"], recursive=False)]


def race_info(text, race, d, venue):
    """由頁面文字抽出場次資料（原文，唔轉換）。"""
    m = re.search(rf"第\s*{race}\s*場", text)
    seg = text[m.start(): m.start() + 500] if m else ""

    def g(p):
        x = re.search(p, seg)
        return x.group(1) if x else ""

    return {
        "日期": f"{d:%Y/%m/%d}", "場地": venue, "場次": race,
        "賽事名稱": g(r"第\s*\d+\s*場\s*-\s*(\S+)"),
        "開跑時間": g(r"\b(\d{1,2}:\d{2})\b"),
        "跑道": g(r"(草地|全天候(?:跑道)?)"),
        "賽道": g(r'"([^"]+)"\s*賽道'),
        "路程": g(r"(\d{3,4})\s*米"),
        "場地狀況": g(r"場地狀況\s*:\s*(\S+)") or g(r"(好地至快地|好地至黏地|快地|好地|黏地|軟地|濕快|濕慢|大爛地|爛地)"),
        "班次": g(r"(第[一二三四五]班|國際一級賽|[一二三]級賽|新馬賽|四歲馬賽)"),
        "評分範圍": g(r"評分:\s*([\d\-]+)") or g(r"\((\d+-\d+)\)"),
        "獎金": g(r"(?:獎金:\s*|HK)\$\s*([\d,]+)").replace(",", ""),
    }, seg


# ---------- 排位表 ----------
def card_url(d, code, race):
    return f"{BASE}/racecard?racedate={d:%Y/%m/%d}&Racecourse={code}&RaceNo={race}"


def parse_card(html, race, d, venue):
    soup = BeautifulSoup(html, "html.parser")
    info, seg = race_info(T(soup.get_text(" ")), race, d, venue)
    nums = [int(x) for x in re.findall(r"RaceNo=(\d+)", html)]
    info["_max"] = max(nums + [race])
    head, rows = find_table(soup, ["馬名", "檔位", "騎師"])
    horses = []
    if head is None:
        return info, seg, horses, head
    for tr in rows:
        vals = cells(tr)
        if len(vals) < len(head) - 2 or not re.fullmatch(r"\d+", vals[0] if vals else ""):
            continue
        a = tr.find("a", href=re.compile(r"horseid=", re.I))
        h = {"馬匹ID": re.search(r"horseid=([A-Za-z0-9_]+)", a["href"]).group(1) if a else ""}
        for c in CARD_HORSE_COLS:
            h[c] = vals[head.index(c)] if c in head and head.index(c) < len(vals) else ""
        horses.append(h)
    return info, seg, horses, head


# ---------- 馬匹往績 ----------
def horse_url(hid):
    return f"{BASE}/horse?horseid={hid}&Option=1"   # Option=1 = 所有往績（預設只顯示近三季）


def parse_history(html):
    """回傳香港賽績原始行（馬場以沙田或跑馬地開頭）；其他（海外）唔記。"""
    soup = BeautifulSoup(html, "html.parser")
    head, rows = find_table(soup, ["名次", "日期", "途程"])
    if head is None:
        return None
    out = []
    for tr in rows:
        vals = cells(tr)
        if len(vals) < len(head) - 3:
            continue
        r = {c: (vals[head.index(c)] if c in head and head.index(c) < len(vals) else "") for c in HIST_HORSE_COLS}
        if not re.fullmatch(r"\d\d/\d\d/\d\d", r["日期"]):
            continue
        if not (r["馬場/跑道/賽道"].startswith("沙田") or r["馬場/跑道/賽道"].startswith("跑馬地")):
            continue
        out.append(r)
    return out


# ---------- 賽果 ----------
def results_url(d, code, race):
    return f"{BASE}/localresults?racedate={d:%Y/%m/%d}&Racecourse={code}&RaceNo={race}"


def parse_results(html, race, d, venue):
    soup = BeautifulSoup(html, "html.parser")
    text = T(soup.get_text(" "))
    if not re.search(rf"第\s*{race}\s*場", text):
        return None, None
    info = {k: "" for k in RACE_COLS} | {"日期": f"{d:%Y/%m/%d}", "場地": venue, "場次": race}
    m = re.search(rf"第\s*{race}\s*場\s*\((\d+)\)\s*(.*?)\s*場地狀況\s*:\s*(\S+)\s*(.*?)\s*賽道\s*:\s*(.*?)\s*HK\$\s*([\d,]+)", text)
    if m:
        parts = [x.strip() for x in m.group(2).split(" - ")]
        track = m.group(5)
        info.update({"班次": parts[0] if parts else "", "路程": re.sub(r"\D", "", parts[1]) if len(parts) > 1 else "",
                     "評分範圍": parts[2].strip("()") if len(parts) > 2 else "", "場地狀況": m.group(3),
                     "賽事名稱": m.group(4), "跑道": "全天候" if "全天候" in track else ("草地" if "草地" in track else track),
                     "賽道": (re.search(r'"([^"]+)"', track) or [None, ""])[1], "獎金": m.group(6).replace(",", "")})
    head, rows = find_table(soup, ["名次", "馬號", "馬名"])
    if head is None:
        return info, None
    out = []
    for tr in rows:
        vals = cells(tr)
        if len(vals) < len(head) - 2 or not vals[0]:
            continue
        out.append({c: (vals[head.index(c)] if c in head and head.index(c) < len(vals) else "") for c in RESULT_HORSE_COLS})
    return info, out


def parse_dividends(html):
    soup = BeautifulSoup(html, "html.parser")
    head, rows = find_table(soup, ["彩池", "勝出組合"])
    if head is None:
        return []
    out, pool = [], ""
    for tr in rows:
        vals = cells(tr)
        if not vals or re.search(r"騎師王|練馬師王|派彩備註", " ".join(vals)):
            continue
        if len(vals) >= 3:
            pool, combo, pay = vals[0], vals[1], vals[2]
        elif len(vals) == 2:
            combo, pay = vals
        else:
            continue
        unit = re.search(r"/\$([\d.]+)", pay)
        amt = re.match(r"[\d,]+(?:\.\d+)?", pay)
        if not amt:
            continue
        out.append({"彩池": pool, "勝出組合": combo, "派彩": amt.group(0).replace(",", ""),
                    "單位": f"每${float(unit.group(1)):g}" if unit else "每$10"})
    return out


# ---------- 存檔 ----------
def season_file(d, name):
    return R.DATA / f"{R.season_name(d)}_{name}.csv"


def read_csv(p):
    if not p.exists():
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_csv(p, cols, rows):
    if not rows:
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"  寫入 {len(rows)} 行 -> {p.name}")
    return len(rows)


# ---------- 主要動作 ----------
def do_card(d, code):
    venue = R.VENUES[code]
    ds = f"{d:%Y/%m/%d}"
    p_card = season_file(d, "排位表")
    p_hist = R.DATA / "馬匹往績.csv"
    done = {r["場次"] for r in read_csv(p_card) if r["日期"] == ds}
    have_hist = {(r["馬匹ID"], r["往績日期"], r["往績場次編號"]) for r in read_csv(p_hist)}
    n, last, total = 1, None, 0
    while n <= 14:
        if last is not None and n > last:
            break
        if str(n) in done:
            n += 1
            continue
        html = fetch(card_url(d, code, n), "馬名")
        info, seg, horses, head = parse_card(html, n, d, venue)
        last = info["_max"] if last is None else max(last, info["_max"])
        if n > info["_max"] or not horses:
            if n == 1:
                raise RuntimeError(f"第 1 場排位表讀唔到馬匹（{card_url(d, code, 1)}）")
            break
        missing = [c for c in CARD_HORSE_COLS if c not in head]
        if missing:
            R.log_issue("排位表", f"{ds} 第{n}場", f"排位表冇以下欄位：{'、'.join(missing)}")
        if not info["開跑時間"] or not info["路程"]:
            R.log_issue("排位表", f"{ds} 第{n}場", f"讀唔到開跑時間或路程：{seg[:80]}")
        ts = stamp()
        card_rows, hist_rows = [], []
        for h in horses:
            card_rows.append({"記錄時間HKT": ts, **{k: info[k] for k in RACE_COLS}, **h})
            if not h["馬匹ID"]:
                R.log_issue("排位表", f"{ds} 第{n}場", f"{h['馬匹編號']}號{h['馬名']}冇馬匹連結，讀唔到往績")
                continue
            hist = parse_history(fetch(horse_url(h["馬匹ID"]), "名次"))
            if hist is None:
                R.log_issue("馬匹往績", h["馬匹ID"], f"{h['馬名']} 往績頁讀唔到往績表")
                continue
            for r in hist:
                k = (h["馬匹ID"], r["日期"], r["場次"])
                if k in have_hist:
                    continue
                have_hist.add(k)
                hist_rows.append({"記錄時間HKT": ts, "馬匹ID": h["馬匹ID"], "馬名": h["馬名"],
                                  **{HIST_RENAME.get(c, c): v for c, v in r.items()}})
        append_csv(p_card, CARD_COLS, card_rows)
        append_csv(p_hist, HIST_COLS, hist_rows)
        R.log_issue("排位表", f"{ds} 第{n}場", f"{len(card_rows)} 匹，新增往績 {len(hist_rows)} 行", ok=True)
        R.push_now(f"排位表第{n}場")
        total += len(card_rows)
        n += 1
    print(f"排位表完成，共 {total} 匹")


def do_results(d, code, races):
    venue = R.VENUES[code]
    ds = f"{d:%Y/%m/%d}"
    p = season_file(d, "賽果")
    done = {r["場次"] for r in read_csv(p) if r["日期"] == ds}
    complete, wrote = True, False
    for n in races:
        if str(n) in done:
            continue
        html = fetch(results_url(d, code, n), "名次")
        info, rows = parse_results(html, n, d, venue)
        divs = parse_dividends(html)
        if not rows or not any(re.match(r"1\b", r["名次"]) for r in rows) or not any(x["彩池"] == "獨贏" for x in divs):
            print(f"  第 {n} 場賽果／派彩未出")
            complete = False
            continue
        ts = stamp()
        append_csv(p, RESULT_COLS, [{"記錄時間HKT": ts, **{k: info[k] for k in RACE_COLS}, **r} for r in rows])
        append_csv(season_file(d, "派彩"), DIV_COLS,
                   [{"記錄時間HKT": ts, "日期": ds, "場地": venue, "場次": n, **x} for x in divs])
        R.log_issue("賽果", f"{ds} 第{n}場", f"{len(rows)} 匹，派彩 {len(divs)} 行", ok=True)
        wrote = True
    if wrote:
        R.push_now("賽果及派彩")
    return complete


def need(now):
    st = R.load_state(now.date())
    if not st or not st.get("race_day"):
        return False
    if not st.get("card_done") and R.hm(now) >= R.hm(datetime(2000, 1, 1, *R.EARLY_FROM)):
        return True
    return bool(st.get("trend_done")) and not st.get("results_done")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    now = R.now_hkt()
    print(f"香港時間 {now:%Y/%m/%d %H:%M:%S}，排位表模式 {mode}")
    if mode == "need":
        v = need(now)
        print(f"排位表／賽果需要做嘢：{v}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"need_rc={'true' if v else 'false'}\n")
        return
    try:
        if mode in ("card", "results"):
            d = datetime.strptime(sys.argv[2], "%Y/%m/%d").date()
            code = sys.argv[3].upper()
            if mode == "card":
                do_card(d, code)
            else:
                races = sorted({int(r["場次"]) for r in read_csv(season_file(d, "排位表")) if r["日期"] == f"{d:%Y/%m/%d}"})
                do_results(d, code, races or range(1, 12))
            return
        d = now.date()
        st = R.load_state(d)
        if not need(now):
            print("排位表／賽果今次唔使做嘢。")
            return
        if not st.get("card_done"):
            do_card(d, st["code"])
            st = R.load_state(d) | {"card_done": True}
            R.save_state(d, st)
        if st.get("trend_done") and not st.get("results_done"):
            if do_results(d, st["code"], st["races"]):
                st["results_done"] = True
                R.save_state(d, st)
    finally:
        close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"錯誤：{e}")
        sys.exit(1)
