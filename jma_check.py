#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
気象庁 警報チェッカー（GitHub Actions 用・1回実行型）
=====================================================
10分ごとにGitHub Actionsから実行される想定。常時接続はしない。
  1. 気象庁 r8 警報JSON(宮城=040000)を取得
  2. 対象15市町村の発表中警報を判定
  3. 大雨・土砂・高潮・氾濫でレベル2以上の発表状況が「前回から変化」したら
     Discord Webhook へ投稿（新規発表 / 全解除）
  4. 状態を state.json に保存（Actions側でcommitして次回に引き継ぐ）

依存ライブラリなし（標準ライブラリのみ）。
Webhook URLは環境変数 DISCORD_WEBHOOK_URL から読む（コードに書かない）。
"""

import os
import re
import json
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta

OFFICE_CODE = os.getenv("JMA_OFFICE_CODE", "040000")
WARNING_URL = f"https://www.jma.go.jp/bosai/warning/data/r8/{OFFICE_CODE}.json"
FLOOD_URL = "https://www.jma.go.jp/bosai/flood/data/r8/flood_xml.json"  # 氾濫（河川ごと・全国）
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
STATE_FILE = os.getenv("STATE_FILE", "state.json")
UA = {"User-Agent": "MiyagiWarnCheck/1.0 (personal use)"}
JST = timezone(timedelta(hours=9))

# 監視対象（discover_areas.py で確定済みコード）
TARGET_GROUPS = {
    "東部石巻地域":       {"石巻市": "0420200", "東松島市": "0421400", "女川町": "0458100"},
    "東部大崎地域":       {"大崎市東部": "0421501", "涌谷町": "0450100", "美里町": "0450500"},
    "東部気仙沼地域":     {"気仙沼市": "0420500", "南三陸町": "0460600"},
    "東部仙南地域":       {"角田市": "0420800", "大河原町": "0432100", "村田町": "0432200",
                          "柴田町": "0432300", "丸森町": "0434100"},
    "東部登米・栗原地域": {"登米市": "0421200", "栗原市東部": "0421301"},
}
CODE_TO_NAME = {code: nm for m in TARGET_GROUPS.values() for nm, code in m.items()}
TRIGGER_DISASTERS = {"大雨", "土砂災害", "高潮", "氾濫", "洪水"}

# code -> (表示名, 災害, レベル, 公式レベルか)
WARNING_TABLE = {
    "10": ("大雨注意報", "大雨", 2, True), "03": ("大雨警報", "大雨", 3, True),
    "43": ("大雨危険警報", "大雨", 4, True), "33": ("大雨特別警報", "大雨", 5, True),
    "19": ("高潮注意報", "高潮", 2, True), "08": ("高潮警報", "高潮", 3, True),
    "48": ("高潮危険警報", "高潮", 4, True), "38": ("高潮特別警報", "高潮", 5, True),
    "29": ("土砂災害注意報", "土砂災害", 2, True), "09": ("土砂災害警報", "土砂災害", 3, True),
    "49": ("土砂災害危険警報", "土砂災害", 4, True), "39": ("土砂災害特別警報", "土砂災害", 5, True),
    "04": ("洪水警報", "洪水", 3, True), "18": ("洪水注意報", "洪水", 2, True),
    # 氾濫（河川ごと・flood_xml由来。合成コードF2〜F5で表す）
    "F2": ("氾濫注意報", "氾濫", 2, True), "F3": ("氾濫警報", "氾濫", 3, True),
    "F4": ("氾濫危険警報", "氾濫", 4, True), "F5": ("氾濫特別警報", "氾濫", 5, True),
    "02": ("暴風雪警報", "暴風雪", 3, False), "05": ("暴風警報", "暴風", 3, False),
    "06": ("大雪警報", "大雪", 3, False), "07": ("波浪警報", "波浪", 3, False),
    "12": ("大雪注意報", "大雪", 2, False), "13": ("風雪注意報", "風雪", 2, False),
    "14": ("雷注意報", "雷", 2, False), "15": ("強風注意報", "強風", 2, False),
    "16": ("波浪注意報", "波浪", 2, False), "17": ("融雪注意報", "融雪", 2, False),
    "20": ("濃霧注意報", "濃霧", 2, False), "21": ("乾燥注意報", "乾燥", 2, False),
    "22": ("なだれ注意報", "なだれ", 2, False), "23": ("低温注意報", "低温", 2, False),
    "24": ("霜注意報", "霜", 2, False), "25": ("着氷注意報", "着氷", 2, False),
    "26": ("着雪注意報", "着雪", 2, False), "32": ("暴風雪特別警報", "暴風雪", 5, False),
    "35": ("暴風特別警報", "暴風", 5, False), "36": ("大雪特別警報", "大雪", 5, False),
    "37": ("波浪特別警報", "波浪", 5, False),
}
LEVEL_LABEL = {2: "注意報", 3: "警報", 4: "危険警報", 5: "特別警報"}


def info(wc):
    return WARNING_TABLE.get(wc, (f"不明警報(コード{wc})", f"不明{wc}", 3, False))


def parse_flood(flood):
    """氾濫(flood_xml)を {対象コード: [合成コードF2..F5]} に。河川のclass20Codesで突き合わせ。"""
    out = {code: [] for code in CODE_TO_NAME}
    for entry in (flood if isinstance(flood, list) else []):
        item = entry.get("item") or {}
        name = unicodedata.normalize("NFKC", item.get("name") or item.get("condition") or "")
        if "解除" in name:
            continue
        mlv = re.search(r"レベル([2-5])", name)
        if not mlv:
            continue
        syn = f"F{mlv.group(1)}"
        for c in entry.get("class20Codes", []):
            if c in out and syn not in out[c]:
                out[c].append(syn)
    return out


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def parse(products):
    result = {code: [] for code in CODE_TO_NAME}
    report_dts = []
    for prod in (products if isinstance(products, list) else [products]):
        if not isinstance(prod, dict):
            continue
        if prod.get("reportDatetime"):
            report_dts.append(prod["reportDatetime"])
        for item in (prod.get("warning") or {}).get("class20Items", []):
            code = item.get("areaCode")
            if code not in result:
                continue
            for k in item.get("kinds", []):
                wc, st = k.get("code"), k.get("status", "")
                if not wc or "解除" in st or st == "発表警報・注意報はなし":
                    continue
                if wc not in result[code]:
                    result[code].append(wc)
    return result, (max(report_dts) if report_dts else "")


def signature(cur):
    items = []
    for code, wcs in cur.items():
        for wc in wcs:
            _, disaster, lv, _ = info(wc)
            if disaster in TRIGGER_DISASTERS and lv >= 2:
                items.append(f"{code}:{wc}")
    return ",".join(sorted(items))


def active_blocks(cur):
    blocks = []
    for code, wcs in cur.items():
        if not wcs:
            continue
        infos = [info(wc) for wc in wcs]
        if not any(d in TRIGGER_DISASTERS and lv >= 2 for _, d, lv, _ in infos):
            continue
        blocks.append((max(lv for _, _, lv, _ in infos), CODE_TO_NAME[code], infos))
    blocks.sort(key=lambda x: x[0], reverse=True)
    return blocks


def fmt_dt(iso):
    if not iso:
        return "--"
    try:
        d = datetime.fromisoformat(iso).astimezone(JST)
        return f"{d.month}/{d.day} {d.hour}:{d.minute:02d}"
    except ValueError:
        return iso


def compose_push(blocks, report_dt):
    now = datetime.now(JST)
    out = ["🌧 **警報・注意報が発表されています**"]
    for max_lv, name, infos in blocks:
        out.append("")
        out.append(f"**{name}は現在レベル{max_lv} {LEVEL_LABEL.get(max_lv,'')}**")
        out.append(f"現在 発表中の全情報　{name}")
        for w_name, disaster, lv, official in sorted(infos, key=lambda x: -x[2]):
            out.append(f"・{disaster} レベル{lv} {LEVEL_LABEL.get(lv,'')}" if official else f"・{w_name}")
    out.append("")
    out.append(f"気象庁発表 {fmt_dt(report_dt)} ／ 問い合わせ {now.month}/{now.day} {now.hour}:{now.minute:02d}")
    return "\n".join(out)


def split(text, limit=1900):
    chunks, buf, cur = [], [], 0
    for ln in text.split("\n"):
        if cur + len(ln) + 1 > limit and buf:
            chunks.append("\n".join(buf)); buf, cur = [], 0
        buf.append(ln); cur += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def post_discord(text):
    for chunk in split(text):
        data = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(WEBHOOK, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"signature": "", "date": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def main():
    if not WEBHOOK:
        sys.exit("環境変数 DISCORD_WEBHOOK_URL が未設定です。")
    products = get_json(WARNING_URL)
    cur, report_dt = parse(products)
    # 氾濫（別ファイル）を取得してマージ。失敗しても本体は継続。
    try:
        fmap = parse_flood(get_json(FLOOD_URL))
        for c, syns in fmap.items():
            for s in syns:
                if s not in cur[c]:
                    cur[c].append(s)
    except Exception as e:
        print("氾濫データ取得失敗（スキップ）:", e)
    sig = signature(cur)
    state = load_state()
    today = datetime.now(JST).strftime("%Y-%m-%d")

    # 手動実行(Run workflow)時: 変化に関係なく現況を投稿（簡易「今の警報」）
    if os.getenv("FORCE_POST", "") == "1":
        blocks = active_blocks(cur)
        post_discord(compose_push(blocks, report_dt) if blocks
                     else "📋 現在、対象地域で大雨・土砂・高潮・氾濫の警報・注意報は発表なしです。")
        save_state({"signature": sig, "date": today})
        print("現況を強制投稿")
        return

    if sig != state.get("signature", ""):
        blocks = active_blocks(cur)
        if blocks:
            post_discord(compose_push(blocks, report_dt))
            print("通知を送信:", sig)
        elif state.get("signature"):
            post_discord("✅ 対象地域の大雨・土砂・高潮・氾濫の警報・注意報はすべて解除されました。")
            print("全解除を送信")
    else:
        print("変化なし:", sig or "(発表なし)")

    save_state({"signature": sig, "date": today})   # dateが日次で変わる＝リポジトリ活性維持


if __name__ == "__main__":
    main()
