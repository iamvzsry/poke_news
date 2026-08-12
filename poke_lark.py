#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poke_lark.py — Pokemon 情報を Lark(飛書) webhook に流す通知スクリプト

対象:
  - カード発売 / 新弾情報 (ポケモンカード公式, PokeBeach)
  - 抽選販売・予約 (ポケカ速報系ブログ)
  - イベント (オンライン / オフライン, 公式イベント告知)
  - ゲーム関連ニュース (本編 / GO / Pokopia など)

設計方針:
  - 標準ライブラリのみ (pip install 不要 → Lambda / GitHub Actions / cron にそのまま置ける)
  - RSS(2.0 / RDF / Atom) と HTML リンク抽出の 2 方式をコンフィグで切り替え
  - state ファイルで既読 ID を保持し重複通知を防ぐ
  - タイトルのキーワードでカテゴリ分類 → Lark カードにグルーピング表示

Usage:
  export LARK_WEBHOOK_URL="https://open.larksuite.com/open-apis/bot/v2/hook/xxxx"
  export LARK_WEBHOOK_SECRET="..."      # 署名検証を有効にしている場合のみ
  export LARK_WEBHOOK_KEYWORD="ポケモン"  # キーワード検証を有効にしている場合のみ

  python3 poke_lark.py                    # 通常実行
  python3 poke_lark.py --dry-run          # 送信せず標準出力
  python3 poke_lark.py --seed             # 初回: 既読だけ記録して通知しない
  python3 poke_lark.py --source pokecard  # 単一ソースのみ
  python3 poke_lark.py --debug-source pokecard   # パース結果を生ダンプ(セレクタ調整用)
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Iterable

JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; poke-lark-notifier/1.0; +personal use)"

LOG = logging.getLogger("poke_lark")


# --------------------------------------------------------------------------
# ソース定義
# --------------------------------------------------------------------------
# kind:
#   "rss"  -> RSS2.0 / RSS1.0(RDF) / Atom を自動判別
#   "html" -> href_pattern に一致する <a> を記事とみなす
#
# href_pattern を変えるだけで他サイトにも流用できる。
# 公式サイトは HTML 構造が変わることがあるので、取れなくなったら
#   --debug-source <name> で実際に抽出されたリンクを確認して調整する。
# --------------------------------------------------------------------------
SOURCES: list[dict] = [
    {
        "name": "pokecard",
        "label": "ポケカ公式",
        "kind": "html",
        "url": "https://www.pokemon-card.com/info/",
        "href_pattern": r"^/info/(?:\d+|detail/\d+)/?$",
        "enabled": True,
    },
    {
        "name": "pokemon-jp",
        "label": "ポケモン公式",
        "kind": "html",
        "url": "https://www.pokemon.co.jp/info/",
        "href_pattern": r"^/info/\d+",
        "enabled": True,
    },
    {
        "name": "pokecainfo",
        "label": "ポケカ抽選速報",
        "kind": "rss",
        # livedoor blog は index.rdf / atom.xml を持つ
        "url": "https://pokecainfo.livedoor.blog/index.rdf",
        "enabled": True,
    },
    {
        "name": "pokebeach",
        "label": "PokeBeach",
        "kind": "rss",
        "url": "https://www.pokebeach.com/feed",
        "enabled": True,
    },
    {
        "name": "pokemondb",
        "label": "PokemonDB",
        "kind": "rss",
        "url": "https://pokemondb.net/news/feed",
        "enabled": False,  # 英語ゲーム系。必要なら True
    },
    {
        "name": "pokemonblog",
        "label": "Pokemon Blog",
        "kind": "rss",
        "url": "https://pokemonblog.com/feed/",
        "enabled": False,
    },
]

# --------------------------------------------------------------------------
# カテゴリ分類 (上から順に評価、最初にマッチしたものを採用)
# --------------------------------------------------------------------------
CATEGORIES: list[tuple[str, str, list[str]]] = [
    (
        "lottery",
        "🎫 抽選・予約",
        ["抽選", "応募", "受注", "予約", "先行販売", "招待販売", "再販",
         "lottery", "pre-?order", "raffle"],
    ),
    (
        "card",
        "🃏 カード / 新弾",
        ["拡張パック", "強化拡張", "ハイクラス", "スターターセット", "構築デッキ",
         "カードリスト", "収録カード", "発売", "新商品", "プロモ", "サプライ",
         "booster", "expansion", "set list", "tcg", "card"],
    ),
    (
        "event",
        "📍 イベント",
        ["イベント", "大会", "チャンピオンシップ", "シティリーグ", "ジムバトル",
         "トレーナーズリーグ", "カードゲーム教室", "体験会", "ポケモンセンター",
         "ストア", "コラボ", "開催", "出展", "championship", "event", "tournament"],
    ),
    (
        "game",
        "🎮 ゲーム",
        ["ポケモンGO", "Pokémon GO", "Pokopia", "スカーレット", "バイオレット",
         "アップデート", "配信", "配布", "シリアルコード", "ふしぎなおくりもの",
         "Nintendo Switch", "DLC", "update", "distribution", "patch"],
    ),
]
DEFAULT_CATEGORY = ("other", "📰 その他")

CATEGORY_ORDER = ["lottery", "card", "event", "game", "other"]

# 抽選系はヘッダー色を変えて目立たせる
HEADER_TEMPLATE = {
    "lottery": "red",
    "card": "orange",
    "event": "green",
    "game": "blue",
    "other": "grey",
}


# --------------------------------------------------------------------------
# データモデル
# --------------------------------------------------------------------------
@dataclass
class Item:
    source: str
    label: str
    title: str
    url: str
    published: str | None = None
    category: str = "other"
    category_label: str = DEFAULT_CATEGORY[1]

    @property
    def uid(self) -> str:
        base = self.url or f"{self.source}:{self.title}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:20]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url: str, timeout: int = 20, retries: int = 3) -> str:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.8",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                charset = resp.headers.get_content_charset()
                if not charset:
                    head = raw[:2048].decode("ascii", "ignore").lower()
                    m = re.search(r'charset=["\']?([\w\-]+)', head)
                    charset = m.group(1) if m else "utf-8"
                return raw.decode(charset, errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            LOG.warning("GET failed (%d/%d) %s: %s", attempt, retries, url, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"fetch failed: {url}: {last_err}")


# --------------------------------------------------------------------------
# パーサ
# --------------------------------------------------------------------------
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_feed(xml_text: str, src: dict) -> list[Item]:
    """RSS2.0 / RSS1.0(RDF) / Atom を一律で処理する。"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as e:
        LOG.error("XML parse error for %s: %s", src["name"], e)
        return []

    items: list[Item] = []
    for node in root.iter():
        if _strip_ns(node.tag) not in ("item", "entry"):
            continue

        title = link = pub = None
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "title" and child.text:
                title = child.text.strip()
            elif tag == "link":
                # Atom は href 属性、RSS はテキスト
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                elif child.text and child.text.strip():
                    link = child.text.strip()
            elif tag in ("pubDate", "published", "updated", "date") and child.text:
                pub = child.text.strip()

        if title and link:
            items.append(Item(src["name"], src["label"], clean_text(title), link, pub))
    return items


class LinkHarvester(HTMLParser):
    """<a href> とその内側テキストを収集する軽量パーサ。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._depth = 0
        self._href = ""
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            if self._depth:
                pass
            return
        href = dict(attrs).get("href")
        if self._depth:
            self._depth += 1
            return
        if href:
            self._depth = 1
            self._href = href
            self._buf = []

    def handle_endtag(self, tag):
        if tag != "a" or not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            text = clean_text("".join(self._buf))
            self.links.append((self._href, text))
            self._href, self._buf = "", []

    def handle_data(self, data):
        if self._depth:
            self._buf.append(data)


def parse_html(html_text: str, src: dict) -> list[Item]:
    harvester = LinkHarvester()
    try:
        harvester.feed(html_text)
    except Exception as e:  # noqa: BLE001
        LOG.error("HTML parse error for %s: %s", src["name"], e)
        return []

    pattern = re.compile(src["href_pattern"])
    origin = re.match(r"^(https?://[^/]+)", src["url"]).group(1)

    items: list[Item] = []
    seen: set[str] = set()
    for href, text in harvester.links:
        path = href
        if href.startswith("http"):
            if not href.startswith(origin):
                continue
            path = href[len(origin):]
        elif not href.startswith("/"):
            continue

        if not pattern.search(path):
            continue
        if len(text) < 4:  # 「詳しくはこちら」等の空リンク除去
            continue

        full = origin + path
        if full in seen:
            continue
        seen.add(full)
        items.append(Item(src["name"], src["label"], text, full))
    return items


def clean_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def classify(item: Item) -> Item:
    hay = item.title.lower()
    for key, label, keywords in CATEGORIES:
        for kw in keywords:
            if re.search(kw.lower(), hay):
                item.category, item.category_label = key, label
                return item
    item.category, item.category_label = DEFAULT_CATEGORY
    return item


# --------------------------------------------------------------------------
# 既読状態
# --------------------------------------------------------------------------
class State:
    """{uid: epoch} の単純な JSON。RETENTION_DAYS を過ぎたものは自動削除。"""

    RETENTION_DAYS = 60

    def __init__(self, path: str) -> None:
        self.path = path
        self.seen: dict[str, float] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.seen = json.load(f)
            except Exception as e:  # noqa: BLE001
                LOG.warning("state read failed, starting fresh: %s", e)

    def is_new(self, uid: str) -> bool:
        return uid not in self.seen

    def mark(self, uid: str) -> None:
        self.seen[uid] = time.time()

    def save(self) -> None:
        cutoff = time.time() - self.RETENTION_DAYS * 86400
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.seen, f)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------
# Lark 通知
# --------------------------------------------------------------------------
class LarkNotifier:
    def __init__(self, webhook: str, secret: str = "", keyword: str = "") -> None:
        if not webhook:
            raise ValueError("LARK_WEBHOOK_URL is required")
        self.webhook = webhook
        self.secret = secret
        self.keyword = keyword

    def _sign(self, ts: int) -> str:
        # Lark カスタム bot の署名検証: key = f"{timestamp}\n{secret}", msg = b""
        key = f"{ts}\n{self.secret}".encode("utf-8")
        digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def send(self, payload: dict, retries: int = 3) -> dict:
        body = dict(payload)
        if self.secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = self._sign(ts)

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(
                self.webhook,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8",
                         "User-Agent": UA},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
                # 0 / StatusCode 0 が成功
                if res.get("code") in (0, None) and res.get("StatusCode", 0) == 0:
                    return res
                raise RuntimeError(f"lark returned: {res}")
            except Exception as e:  # noqa: BLE001
                last_err = e
                LOG.warning("webhook post failed (%d/%d): %s", attempt, retries, e)
                if attempt < retries:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"webhook failed after {retries} attempts: {last_err}")

    # ---- payload builders -------------------------------------------------
    def build_card(self, items: list[Item]) -> dict:
        now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
        grouped: dict[str, list[Item]] = {}
        for it in items:
            grouped.setdefault(it.category, []).append(it)

        title = f"ポケモン最新情報 {len(items)}件"
        if self.keyword and self.keyword not in title:
            title = f"{self.keyword} / {title}"

        # 抽選が含まれていればヘッダーを赤に
        top_cat = next((c for c in CATEGORY_ORDER if c in grouped), "other")

        elements: list[dict] = []
        for cat in CATEGORY_ORDER:
            if cat not in grouped:
                continue
            label = grouped[cat][0].category_label
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md", "content": f"**{label}**"}})
            lines = []
            for it in grouped[cat]:
                t = it.title if len(it.title) <= 90 else it.title[:88] + "…"
                lines.append(f"・[{t}]({it.url})\n　<font color='grey'>{it.label}</font>")
            elements.append({"tag": "div",
                             "text": {"tag": "lark_md", "content": "\n".join(lines)}})
            elements.append({"tag": "hr"})

        if elements and elements[-1].get("tag") == "hr":
            elements.pop()

        elements.append({
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"取得: {now}"}],
        })

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": HEADER_TEMPLATE.get(top_cat, "blue"),
                },
                "elements": elements,
            },
        }

    def build_text(self, items: list[Item]) -> dict:
        head = f"{self.keyword + ' ' if self.keyword else ''}ポケモン最新情報 {len(items)}件"
        lines = [head] + [f"{it.category_label} {it.title}\n{it.url}" for it in items]
        return {"msg_type": "text", "content": {"text": "\n\n".join(lines)}}


# --------------------------------------------------------------------------
# 収集
# --------------------------------------------------------------------------
def collect(src: dict) -> list[Item]:
    LOG.info("fetching %s (%s)", src["name"], src["url"])
    text = http_get(src["url"])
    items = parse_feed(text, src) if src["kind"] == "rss" else parse_html(text, src)
    LOG.info("  -> %d items", len(items))
    return [classify(i) for i in items]


def run(args: argparse.Namespace) -> int:
    sources = [s for s in SOURCES if s.get("enabled", True)]
    if args.source:
        sources = [s for s in SOURCES if s["name"] in args.source]
        if not sources:
            LOG.error("no source matched: %s", args.source)
            return 2

    all_items: list[Item] = []
    for src in sources:
        try:
            all_items.extend(collect(src))
        except Exception as e:  # noqa: BLE001
            LOG.error("source %s failed: %s", src["name"], e)

    if args.debug_source:
        for it in all_items:
            print(f"[{it.category:8s}] {it.label:12s} {it.title}\n           {it.url}")
        return 0

    state = State(args.state)
    fresh = [i for i in all_items if state.is_new(i.uid)]

    # ソース側の並び順を保ちつつ、抽選 > カード > イベント > ゲーム の優先度で並べ替え
    fresh.sort(key=lambda i: CATEGORY_ORDER.index(i.category))
    if args.limit:
        fresh = fresh[: args.limit]

    if args.seed:
        for i in all_items:
            state.mark(i.uid)
        state.save()
        LOG.info("seeded %d items, no notification sent", len(all_items))
        return 0

    if not fresh:
        LOG.info("no new items")
        return 0

    LOG.info("%d new items", len(fresh))

    notifier = LarkNotifier(
        os.environ.get("LARK_WEBHOOK_URL", ""),
        os.environ.get("LARK_WEBHOOK_SECRET", ""),
        os.environ.get("LARK_WEBHOOK_KEYWORD", ""),
    ) if not args.dry_run else None

    payload_builder = (lambda x: notifier.build_text(x)) if args.text else (
        lambda x: notifier.build_card(x))

    if args.dry_run:
        stub = LarkNotifier("https://dry-run.invalid",
                            keyword=os.environ.get("LARK_WEBHOOK_KEYWORD", ""))
        pay = stub.build_text(fresh) if args.text else stub.build_card(fresh)
        print(json.dumps(pay, ensure_ascii=False, indent=2))
        return 0

    # Lark カードは長すぎると弾かれるので 15 件ずつ分割送信
    CHUNK = 15
    for i in range(0, len(fresh), CHUNK):
        chunk = fresh[i:i + CHUNK]
        notifier.send(payload_builder(chunk))
        for it in chunk:
            state.mark(it.uid)
        state.save()   # チャンク毎に保存 → 途中失敗しても二重送信しない
        if i + CHUNK < len(fresh):
            time.sleep(1)

    LOG.info("sent %d items", len(fresh))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Pokemon news -> Lark webhook")
    p.add_argument("--state", default=os.environ.get("POKE_STATE", "./poke_state.json"))
    p.add_argument("--source", action="append", help="対象ソース名(複数可)")
    p.add_argument("--dry-run", action="store_true", help="送信せず payload を出力")
    p.add_argument("--seed", action="store_true", help="初回: 既読登録のみ")
    p.add_argument("--text", action="store_true", help="カードでなくプレーンテキストで送る")
    p.add_argument("--limit", type=int, default=30, help="1回の最大通知件数")
    p.add_argument("--debug-source", action="store_true",
                   help="抽出結果をダンプ(セレクタ調整用)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        LOG.exception("fatal: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
