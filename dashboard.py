#!/usr/bin/env python3
"""
pidash - Raspberry Pi ターミナルダッシュボード
SSH で接続したターミナルに 時計 / 天気 / Spotify 再生状況 / Pi の状態 を表示する。

使い方:
    python3 dashboard.py            # 通常起動
    python3 dashboard.py --auth     # Spotify の初回認証
    python3 dashboard.py --demo     # ネットに繋がずダミーデータで表示確認
終了: Ctrl+C
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import socket
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import requests
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
SPOTIFY_CACHE = BASE_DIR / ".spotify_token_cache"
SPOTIFY_SCOPE = "user-read-playback-state user-read-currently-playing"

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────
DEFAULT_CONFIG = {
    "weather": {"city": "東京", "latitude": 35.6812, "longitude": 139.7671, "interval_sec": 600},
    "spotify": {
        "enabled": True,
        "client_id": "",
        "client_secret": "",
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "interval_sec": 5,
    },
    "news": {
        "enabled": True,
        "feeds": [
            {"name": "Google ニュース", "url": "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"},
        ],
        "interval_sec": 900,   # 取得間隔
        "rotate_sec": 10,      # 見出しを切り替える間隔
        "lines": 5,            # 一度に表示する件数
    },
    "display": {"refresh_per_sec": 4, "accent": "cyan", "show_seconds": True, "pet": "both"},
}


def load_config() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULT_CONFIG.items()}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for section, values in user.items():
            cfg.setdefault(section, {}).update(values)
    # 環境変数でも上書きできるようにしておく（秘密情報をファイルに書きたくない場合用）
    sp = cfg["spotify"]
    sp["client_id"] = os.environ.get("SPOTIFY_CLIENT_ID", sp["client_id"])
    sp["client_secret"] = os.environ.get("SPOTIFY_CLIENT_SECRET", sp["client_secret"])
    return cfg


# ─────────────────────────────────────────────
# 大きい時計用の数字フォント（5行）
# ─────────────────────────────────────────────
BIG_FONT = {
    "0": ["█▀▀█", "█  █", "█  █", "█  █", "█▄▄█"],
    "1": ["  ▄█", "   █", "   █", "   █", "   █"],
    "2": ["█▀▀█", "   █", "█▀▀▀", "█   ", "█▄▄▄"],
    "3": ["█▀▀█", "   █", " ▀▀█", "   █", "█▄▄█"],
    "4": ["█  █", "█  █", "▀▀▀█", "   █", "   █"],
    "5": ["█▀▀▀", "█   ", "▀▀▀█", "   █", "▄▄▄█"],
    "6": ["█▀▀▀", "█   ", "█▀▀█", "█  █", "█▄▄█"],
    "7": ["█▀▀█", "   █", "   █", "   █", "   █"],
    "8": ["█▀▀█", "█  █", "█▀▀█", "█  █", "█▄▄█"],
    "9": ["█▀▀█", "█  █", "▀▀▀█", "   █", "▄▄▄█"],
    ":": ["  ", "▄ ", "  ", "▄ ", "  "],
    " ": ["  ", "  ", "  ", "  ", "  "],
}


def big_text(s: str) -> str:
    rows = ["", "", "", "", ""]
    for ch in s:
        glyph = BIG_FONT.get(ch, BIG_FONT[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "
    return "\n".join(r.rstrip() for r in rows)


# ─────────────────────────────────────────────
# 天気（Open-Meteo: APIキー不要）
# ─────────────────────────────────────────────
WMO_CODES = {
    0: ("☀", "快晴"), 1: ("🌤", "晴れ"), 2: ("⛅", "一部曇り"), 3: ("☁", "曇り"),
    45: ("🌫", "霧"), 48: ("🌫", "霧氷"),
    51: ("🌦", "弱い霧雨"), 53: ("🌦", "霧雨"), 55: ("🌦", "強い霧雨"),
    61: ("🌧", "弱い雨"), 63: ("🌧", "雨"), 65: ("🌧", "強い雨"),
    66: ("🌧", "着氷性の雨"), 67: ("🌧", "強い着氷性の雨"),
    71: ("🌨", "弱い雪"), 73: ("🌨", "雪"), 75: ("🌨", "大雪"), 77: ("🌨", "霧雪"),
    80: ("🌦", "にわか雨"), 81: ("🌧", "強いにわか雨"), 82: ("⛈", "激しいにわか雨"),
    85: ("🌨", "にわか雪"), 86: ("🌨", "強いにわか雪"),
    95: ("⛈", "雷雨"), 96: ("⛈", "雹を伴う雷雨"), 99: ("⛈", "激しい雹と雷雨"),
}


@dataclass
class WeatherState:
    ok: bool = False
    error: str = "取得中…"
    temp: float | None = None
    feels: float | None = None
    humidity: int | None = None
    wind: float | None = None
    code: int | None = None
    t_max: float | None = None
    t_min: float | None = None
    rain_prob: int | None = None
    hourly: list[tuple[str, float, int]] = field(default_factory=list)  # (時刻, 気温, 降水確率)
    sunrise: str = ""
    sunset: str = ""
    updated: dt.datetime | None = None


def fetch_weather(lat: float, lon: float) -> WeatherState:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "timezone": "Asia/Tokyo",
        "forecast_days": 2,
        "wind_speed_unit": "ms",
    }
    # 接続5秒・応答30秒まで待ち、タイムアウトしたら少し空けて最大3回試す
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=(5, 30))
            r.raise_for_status()
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            time.sleep(3 * (attempt + 1))
    else:
        raise last_err  # type: ignore[misc]
    d = r.json()
    cur, daily, hourly = d["current"], d["daily"], d["hourly"]

    now = dt.datetime.now()
    upcoming = []
    for t, temp, prob in zip(hourly["time"], hourly["temperature_2m"], hourly["precipitation_probability"]):
        ts = dt.datetime.fromisoformat(t)
        if ts > now and len(upcoming) < 6:
            upcoming.append((ts.strftime("%H時"), temp, prob or 0))

    return WeatherState(
        ok=True, error="",
        temp=cur["temperature_2m"], feels=cur["apparent_temperature"],
        humidity=cur["relative_humidity_2m"], wind=cur["wind_speed_10m"],
        code=cur["weather_code"],
        t_max=daily["temperature_2m_max"][0], t_min=daily["temperature_2m_min"][0],
        rain_prob=daily["precipitation_probability_max"][0],
        hourly=upcoming[::2] if len(upcoming) > 3 else upcoming,  # 2時間おきに3つ
        sunrise=daily.get("sunrise", [""])[0][-5:],
        sunset=daily.get("sunset", [""])[0][-5:],
        updated=now,
    )


# ─────────────────────────────────────────────
# Spotify（Web API / spotipy）
# ─────────────────────────────────────────────
@dataclass
class SpotifyState:
    status: str = "init"  # init / playing / paused / idle / disabled / error / no_auth
    message: str = ""
    title: str = ""
    artists: str = ""
    album: str = ""
    device: str = ""
    volume: int | None = None
    shuffle: bool = False
    repeat: str = "off"
    progress_ms: int = 0
    duration_ms: int = 0
    fetched_at: float = 0.0  # time.monotonic()

    def live_progress_ms(self) -> int:
        """ポーリング間も進捗バーが滑らかに進むようにローカルで補間する"""
        if self.status != "playing":
            return self.progress_ms
        elapsed = (time.monotonic() - self.fetched_at) * 1000
        return min(int(self.progress_ms + elapsed), self.duration_ms)


def make_spotify_oauth(cfg: dict):
    from spotipy.oauth2 import SpotifyOAuth

    sp = cfg["spotify"]
    return SpotifyOAuth(
        client_id=sp["client_id"],
        client_secret=sp["client_secret"],
        redirect_uri=sp["redirect_uri"],
        scope=SPOTIFY_SCOPE,
        cache_path=str(SPOTIFY_CACHE),
        open_browser=False,  # Pi はヘッドレス想定。URL を表示して手動で開く
    )


def make_spotify_client(cfg: dict):
    import spotipy

    oauth = make_spotify_oauth(cfg)
    if not oauth.get_cached_token():
        return None
    return spotipy.Spotify(auth_manager=oauth, requests_timeout=10, retries=0)


def fetch_spotify(client) -> SpotifyState:
    pb = client.current_playback()
    if not pb or not pb.get("item"):
        return SpotifyState(status="idle", message="再生していません", fetched_at=time.monotonic())

    item = pb["item"]
    if item.get("type") == "episode":  # ポッドキャスト
        artists = item.get("show", {}).get("name", "")
        album = "Podcast"
    else:
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        album = item.get("album", {}).get("name", "")

    device = pb.get("device") or {}
    return SpotifyState(
        status="playing" if pb.get("is_playing") else "paused",
        title=item.get("name", ""),
        artists=artists,
        album=album,
        device=device.get("name", ""),
        volume=device.get("volume_percent"),
        shuffle=pb.get("shuffle_state", False),
        repeat=pb.get("repeat_state", "off"),
        progress_ms=pb.get("progress_ms") or 0,
        duration_ms=item.get("duration_ms") or 0,
        fetched_at=time.monotonic(),
    )


# ─────────────────────────────────────────────
# ニュース（RSS）
# ─────────────────────────────────────────────
@dataclass
class NewsItem:
    title: str
    source: str
    published: dt.datetime | None


@dataclass
class NewsState:
    ok: bool = False
    error: str = "取得中…"
    items: list[NewsItem] = field(default_factory=list)
    updated: dt.datetime | None = None


def _parse_date(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    from email.utils import parsedate_to_datetime
    try:
        d = parsedate_to_datetime(s)  # RSS 2.0 (RFC 822)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom
        except Exception:
            return None
    if d.tzinfo:
        d = d.astimezone().replace(tzinfo=None)
    return d


def fetch_feed(name: str, url: str) -> list[NewsItem]:
    import xml.etree.ElementTree as ET

    r = requests.get(url, timeout=10, headers={"User-Agent": "pidash/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items: list[NewsItem] = []

    # RSS 2.0
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        source = name
        src_el = it.find("source")
        if src_el is not None and src_el.text:  # Google ニュースは記事ごとの媒体名が入っている
            source = src_el.text.strip()
            suffix = f" - {source}"
            if title.endswith(suffix):
                title = title[: -len(suffix)]
        items.append(NewsItem(title, source, _parse_date(it.findtext("pubDate"))))

    # Atom
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
        date = e.findtext("a:updated", default=None, namespaces=ns) or e.findtext("a:published", default=None, namespaces=ns)
        items.append(NewsItem(title, name, _parse_date(date)))

    return [i for i in items if i.title]


def fetch_news(feeds: list[dict]) -> NewsState:
    all_items: list[NewsItem] = []
    errors = []
    for f in feeds:
        try:
            all_items += fetch_feed(f["name"], f["url"])
        except Exception as e:
            errors.append(f"{f['name']}: {type(e).__name__}")
    if not all_items:
        return NewsState(ok=False, error="取得失敗 " + ", ".join(errors))
    # 重複タイトルを除いて新しい順に
    seen, uniq = set(), []
    for i in sorted(all_items, key=lambda x: x.published or dt.datetime.min, reverse=True):
        if i.title not in seen:
            seen.add(i.title)
            uniq.append(i)
    return NewsState(ok=True, error=", ".join(errors), items=uniq[:30], updated=dt.datetime.now())


# ─────────────────────────────────────────────
# ペット（画面下を歩き回るアニメーション）
# ─────────────────────────────────────────────
# スプライトは右向きで定義し、左向きは自動で左右反転する
PET_SPRITES = {
    "cat": {
        # 右向き（しっぽ ~ は進行方向と逆の左側）
        "walk": [
            [r"   /\_/\ ", r"~_( o.o )", r"   /   \ "],
            [r"   /\_/\ ", r"~_( o.o )", r"    | |  "],
        ],
        "sit": [
            [r"   /\_/\ ", r"  ( o.o )", r" ~_(\_/) "],
            [r"   /\_/\ ", r"  ( o.-)", r" ~_(\_/) "],   # ウインク
        ],
        "sleep": [
            [r"          ", r"  __/\_/\ ", r"~(_( -.- )"],
        ],
        "dance": [
            [r"   /\_/\ ", r"  \( ^.^ )", r"   /   \ "],
            [r"   /\_/\ ", r"   ( ^.^ )/", r"    | |  "],
        ],
    },
    "dog": {
        "walk": [
            [r"      __  ", r"\____/ o\_", r" /\  /\   "],
            [r"      __  ", r"\____/ o\_", r"  ||  ||  "],
        ],
        "sit": [
            [r"   __     ", r"  / o\_   ", r" /|  |\_/ "],
            [r"   __     ", r"  / -\_   ", r" /|  |\_/ "],
        ],
        "sleep": [
            [r"          ", r"   __     ", r"\_/ -\___ "],
        ],
        "dance": [
            [r"      __  ", r"/____/ ^\_", r" /\  /\   "],
            [r"      __  ", r"\____/ ^\_", r"  ||  ||  "],
        ],
    },
}

_MIRROR = str.maketrans("()/\\<>[]{}", ")(\\/><][}{")


def _mirror(rows: list[str]) -> list[str]:
    w = max(len(r) for r in rows)
    return [r.ljust(w)[::-1].translate(_MIRROR) for r in rows]


class Pet:
    """1匹ぶんの状態。単独のときは気まぐれに 歩く・座る・寝る・踊る を切り替える"""

    COLORS = {"cat": "#f5a97f", "dog": "#eed49f"}

    def __init__(self, kind: str, rng, x: float = 0.0, direction: int = 1):
        self.kind = kind if kind in PET_SPRITES else "cat"
        self.rng = rng
        self.x = x
        self.dir = direction
        self.state = "walk"
        self.state_until = time.monotonic() + rng.uniform(4, 10)
        self.speed = 6.0 if self.kind == "dog" else 4.0  # 文字/秒
        self.fx = ""
        self.fast = False  # 走っているとき足を速く動かす

    @property
    def w(self) -> int:
        return max(len(r) for r in PET_SPRITES[self.kind]["walk"][0])

    @property
    def center(self) -> float:
        return self.x + self.w / 2

    def clamp(self, width: int):
        self.x = min(max(self.x, 0), max(0, width - self.w))

    def walk(self, dtime: float, width: int, speed: float | None = None, bounce: bool = True) -> bool:
        """歩く。壁に当たったら True"""
        self.state = "walk"
        self.x += self.dir * (speed or self.speed) * dtime
        hit = self.x <= 0 or self.x >= width - self.w
        self.clamp(width)
        if hit and bounce:
            self.dir *= -1
        return hit

    def face(self, other: "Pet"):
        self.dir = 1 if other.center > self.center else -1

    def solo_update(self, now: float, dtime: float, width: int, music: bool, night: bool):
        if now >= self.state_until:
            if night:
                self.state = "sleep" if self.rng.random() < 0.8 else "walk"
            elif music:
                self.state = self.rng.choice(["dance", "dance", "walk", "sit"])
            else:
                self.state = self.rng.choice(["walk", "walk", "walk", "sit"])
            self.state_until = now + self.rng.uniform(4, 12)
            if self.rng.random() < 0.3:
                self.dir *= -1
        if self.state == "walk":
            self.walk(dtime, width)
        self.clamp(width)
        self.fx = ""
        if self.state == "sleep":
            self.fx = ["z", "zZ", "zZz", ""][int(now) % 4]
        elif self.state == "dance" or (music and int(now) % 6 == 0):
            self.fx = ["♪", "♫", "♪♫", ""][int(now * 2) % 4]

    def sprite(self, now: float) -> list[str]:
        frames = PET_SPRITES[self.kind][self.state]
        fps = 8 if self.fast else (2 if self.state in ("sit", "sleep") else 4)
        frame = frames[int(now * fps) % len(frames)]
        return _mirror(frame) if self.dir < 0 else [r.ljust(self.w) for r in frame]


class PetScene:
    """
    ペットたちのレーン。2匹いると、近づいたときにたまに一緒に遊ぶ:
      chase  … 追いかけっこ（壁で折り返すと追う側と逃げる側が入れ替わる）
      ball   … 離れて向かい合い、ボールを投げ合う
      greet  … 鼻をくっつけてあいさつ（♥）
      cuddle … 深夜、寄り添って一緒に寝る
    """

    ROWS = 4  # 0行目: エフェクト / 1〜3行目: スプライト

    def __init__(self, kinds: list[str]):
        import random

        self.rng = random.Random()
        self.pets = [Pet(k, self.rng, x=i * 30.0, direction=1 if i == 0 else -1) for i, k in enumerate(kinds)]
        self.mode: str | None = None
        self.phase = ""
        self.mode_until = 0.0
        self.cooldown_until = time.monotonic() + 8
        self.last = time.monotonic()
        self.ball_t0 = 0.0
        self.runner: Pet | None = None

    # --- 2匹のとき用のヘルパー ---
    def _pair(self) -> tuple[Pet, Pet]:
        a, b = self.pets
        return (a, b) if a.x <= b.x else (b, a)

    def _gap(self) -> float:
        left, right = self._pair()
        return right.x - (left.x + left.w)

    def _start(self, mode: str, now: float, duration: float):
        self.mode, self.phase = mode, "approach"
        self.mode_until = now + duration
        for p in self.pets:
            p.fx, p.fast = "", False
        if mode == "chase":
            self.runner = self.rng.choice(self.pets)
            chaser = self.pets[0] if self.runner is self.pets[1] else self.pets[1]
            self.runner.dir = 1 if self.runner.center > chaser.center else -1
        if mode == "ball":
            self.phase = "spread"

    def _end(self, now: float):
        self.mode = None
        self.cooldown_until = now + self.rng.uniform(10, 25)
        for p in self.pets:
            p.fx, p.fast = "", False
            p.state = "walk"
            p.state_until = now + self.rng.uniform(3, 8)
            if self.rng.random() < 0.5:
                p.dir *= -1

    def _maybe_start(self, now: float, dtime: float, night: bool):
        if len(self.pets) < 2 or now < self.cooldown_until:
            return
        if night:
            if self.rng.random() < dtime * 0.2:
                self._start("cuddle", now, self.rng.uniform(60, 180))
            return
        if any(p.state == "sleep" for p in self.pets):
            return
        if self._gap() < 12 and self.rng.random() < dtime * 0.6:
            mode = self.rng.choice(["chase", "chase", "ball", "ball", "greet"])
            self._start(mode, now, {"chase": 7, "ball": 12, "greet": 4}[mode])

    def _update_mode(self, now: float, dtime: float, width: int, night: bool):
        left, right = self._pair()

        if self.mode == "chase":
            runner = self.runner
            chaser = self.pets[0] if runner is self.pets[1] else self.pets[1]
            runner.fast = chaser.fast = True
            if runner.walk(dtime, width, speed=10, bounce=False):
                # 壁際まで追い詰められたら役割交代
                self.runner, runner, chaser = chaser, chaser, runner
                runner.dir = 1 if runner.center > chaser.center else -1
            else:
                chaser.face(runner)
                if abs(chaser.center - runner.center) > 6:
                    chaser.walk(dtime, width, speed=9.5, bounce=False)
                else:
                    chaser.state = "walk"
            runner.fx, chaser.fx = "!", "!!" if int(now * 3) % 2 else ""

        elif self.mode == "greet":
            left.dir, right.dir = 1, -1
            if self.phase == "approach":
                if self._gap() > 0:
                    left.walk(dtime, width, bounce=False)
                    right.walk(dtime, width, bounce=False)
                else:
                    self.phase = "touch"
            else:
                left.state = right.state = "sit"
                heart = "♥" if int(now * 2) % 2 else "♡"
                left.fx, right.fx = heart, ""

        elif self.mode == "ball":
            if self.phase == "spread":
                # 背中合わせに離れて距離をとる
                left.dir, right.dir = -1, 1
                moved = False
                if self._gap() < 22:
                    if left.x > 0:
                        left.walk(dtime, width, bounce=False)
                        moved = True
                    if right.x < width - right.w:
                        right.walk(dtime, width, bounce=False)
                        moved = True
                if self._gap() >= 22 or not moved:
                    self.phase = "play"
                    self.ball_t0 = now
            else:
                left.dir, right.dir = 1, -1
                period = 1.6
                k = (now - self.ball_t0) / period
                t = k % 1.0
                going_right = int(k) % 2 == 0
                # ボールに近い方が前足を上げてキャッチ
                near_left = (t < 0.2) if going_right else (t > 0.8)
                near_right = (t > 0.8) if going_right else (t < 0.2)
                left.state = "dance" if near_left else "sit"
                right.state = "dance" if near_right else "sit"
                left.fx = right.fx = ""

        elif self.mode == "cuddle":
            left.dir, right.dir = 1, -1
            if self.phase == "approach":
                if self._gap() > 0:
                    left.walk(dtime, width, speed=2.5, bounce=False)
                    right.walk(dtime, width, speed=2.5, bounce=False)
                else:
                    self.phase = "sleep"
            else:
                left.state = right.state = "sleep"
                left.fx = ["z", "zZ", "zZz", ""][int(now) % 4]
                right.fx = ""
            if not night:
                self.mode_until = min(self.mode_until, now)

        for p in self.pets:
            p.clamp(width)
        if now >= self.mode_until:
            self._end(now)

    def _ball_pos(self, now: float) -> tuple[int, int] | None:
        if self.mode != "ball" or self.phase != "play":
            return None
        left, right = self._pair()
        k = (now - self.ball_t0) / 1.6
        t = k % 1.0
        if int(k) % 2:
            t = 1 - t
        x0 = left.x + left.w - 1
        x1 = right.x
        x = x0 + (x1 - x0) * t
        import math
        height = math.sin(math.pi * t) * 3  # 放物線
        row = 3 - round(height)
        return int(x), max(0, min(3, row))

    # --- 描画 ---
    def render(self, width: int, music: bool) -> Text:
        now = time.monotonic()
        dtime, self.last = min(now - self.last, 0.5), now
        hour = dt.datetime.now().hour
        night = 1 <= hour < 6

        if self.mode:
            self._update_mode(now, dtime, width, night)
        else:
            for p in self.pets:
                p.solo_update(now, dtime, width, music, night)
            self._maybe_start(now, dtime, night)

        grid = [[(" ", "")] * width for _ in range(self.ROWS)]

        def put(row: int, col: int, s: str, style: str):
            for i, ch in enumerate(s):
                c = col + i
                if 0 <= c < width and ch != " ":
                    grid[row][c] = (ch, style)

        for p in self.pets:
            xi = int(p.x)
            for r, line in enumerate(p.sprite(now)):
                put(r + 1, xi, line, f"bold {Pet.COLORS[p.kind]}")
            if p.fx:
                put(0, xi + p.w // 2, p.fx, "#8aadf4" if p.fx not in ("♥", "♡") else "#ed8796")

        ball = self._ball_pos(now)
        if ball:
            bx, br = ball
            put(br, bx, "●", "bold #a6da95")

        out = Text()
        for row in grid:
            run, style = "", None
            for ch, st in row:
                if st != style and run:
                    out.append(run, style=style or "")
                    run = ""
                style = st
                run += ch
            out.append(run + "\n", style=style or "")
        out.append("▔" * width, style="dim green")
        return out


# ─────────────────────────────────────────────
# Raspberry Pi 自身の情報
# ─────────────────────────────────────────────
def pi_stats() -> dict:
    stats = {"host": socket.gethostname()}
    try:
        stats["cpu_temp"] = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except Exception:
        stats["cpu_temp"] = None
    try:
        stats["load"] = os.getloadavg()[0]
    except Exception:
        stats["load"] = None
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        stats["uptime"] = f"{int(up // 86400)}日 {int(up % 86400 // 3600)}時間"
    except Exception:
        stats["uptime"] = None
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
        stats["mem"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
    except Exception:
        stats["mem"] = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # 実際には送信しない。経路からIPを得るだけ
        stats["ip"] = s.getsockname()[0]
        s.close()
    except Exception:
        stats["ip"] = None
    return stats


# ─────────────────────────────────────────────
# バックグラウンド更新
# ─────────────────────────────────────────────
class Dashboard:
    def __init__(self, cfg: dict, demo: bool = False, compact: bool = False):
        self.cfg = cfg
        self.demo = demo
        self.compact = compact  # --3ds: 小さい画面向け（カレンダー・ニュース・日の入りなどを省略）
        self.weather = WeatherState()
        self.spotify = SpotifyState()
        self.news = NewsState()
        self.stats: dict = {}
        pet = str(cfg["display"].get("pet", "both")).lower()
        kinds = ["cat", "dog"] if pet == "both" else [pet] if pet in PET_SPRITES else []
        self.pet = PetScene(kinds) if kinds else None
        self.width = 80  # main() で実際のターミナル幅に更新される
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # --- 各ワーカー ---
    def weather_loop(self):
        w = self.cfg["weather"]
        while not self.stop.is_set():
            try:
                state = demo_weather() if self.demo else fetch_weather(w["latitude"], w["longitude"])
            except Exception as e:
                state = WeatherState(ok=False, error=f"取得失敗: {type(e).__name__}")
                with self.lock:  # 失敗しても前回の値は残す
                    if self.weather.ok:
                        self.weather.error = state.error
                        state = self.weather
            with self.lock:
                self.weather = state
            self.stop.wait(w["interval_sec"] if state.ok else 60)

    def spotify_loop(self):
        sp_cfg = self.cfg["spotify"]
        if self.demo:
            demo = demo_spotify()
            with self.lock:
                self.spotify = demo
            return
        if not sp_cfg.get("enabled", True):
            with self.lock:
                self.spotify = SpotifyState(status="disabled", message="Spotify 表示は無効です")
            return
        if not sp_cfg["client_id"] or not sp_cfg["client_secret"]:
            with self.lock:
                self.spotify = SpotifyState(status="no_auth", message="config.toml に client_id / secret を設定してください")
            return
        try:
            client = make_spotify_client(self.cfg)
        except Exception as e:
            client = None
            msg = f"初期化失敗: {e}"
        else:
            msg = "未認証です。`python3 dashboard.py --auth` を実行してください"
        if client is None:
            with self.lock:
                self.spotify = SpotifyState(status="no_auth", message=msg)
            return

        backoff = sp_cfg["interval_sec"]
        while not self.stop.is_set():
            try:
                state = fetch_spotify(client)
                backoff = sp_cfg["interval_sec"]
            except Exception as e:
                state = SpotifyState(status="error", message=f"取得失敗: {type(e).__name__}: {str(e)[:60]}")
                backoff = min(backoff * 2, 120)  # レート制限などに備えて間隔を広げる
            with self.lock:
                self.spotify = state
            self.stop.wait(backoff)

    def news_loop(self):
        n = self.cfg["news"]
        if not n.get("enabled", True) or self.compact:
            return
        while not self.stop.is_set():
            if self.demo:
                state = demo_news()
            else:
                try:
                    state = fetch_news(n["feeds"])
                except Exception as e:
                    state = NewsState(ok=False, error=f"取得失敗: {type(e).__name__}")
                if not state.ok:
                    with self.lock:  # 失敗しても前回の見出しは残す
                        if self.news.ok:
                            self.news.error = state.error
                            state = self.news
            with self.lock:
                self.news = state
            self.stop.wait(n["interval_sec"] if state.ok else 120)

    def stats_loop(self):
        while not self.stop.is_set():
            s = pi_stats()
            with self.lock:
                self.stats = s
            self.stop.wait(5)

    def start(self):
        for fn in (self.weather_loop, self.spotify_loop, self.news_loop, self.stats_loop):
            threading.Thread(target=fn, daemon=True).start()

    # --- 描画 ---
    def render(self, width: int | None = None, height: int | None = None) -> Layout:
        if width:
            self.width = width
        if height:
            self.height = height
        accent = self.cfg["display"]["accent"]
        with self.lock:
            weather, spotify, news, stats = self.weather, self.spotify, self.news, dict(self.stats)
        if self.compact:
            return self.render_compact(weather, spotify, stats, accent)

        news_cfg = self.cfg["news"]
        rows = [
            Layout(name="clock", size=9),
            Layout(name="middle", ratio=1, minimum_size=10),
        ]
        if news_cfg.get("enabled", True):
            rows.append(Layout(name="news", size=news_cfg["lines"] + 2))
        if self.pet:
            rows.append(Layout(name="pet", size=5))
        rows.append(Layout(name="footer", size=3))

        layout = Layout()
        layout.split_column(*rows)
        layout["middle"].split_row(Layout(name="weather", ratio=2), Layout(name="spotify", ratio=3))

        if self.width >= 96:
            layout["clock"].split_row(
                Layout(self.render_today(weather), name="today", size=26),
                Layout(self.render_clock(accent), name="clock_main", ratio=1),
                Layout(self.render_calendar(accent), name="calendar", size=26),
            )
        elif self.width >= 66:
            layout["clock"].split_row(
                Layout(self.render_clock(accent), name="clock_main", ratio=1),
                Layout(self.render_calendar(accent), name="calendar", size=26),
            )
        else:
            layout["clock"].update(self.render_clock(accent))
        layout["weather"].update(self.render_weather(weather, accent))
        layout["spotify"].update(self.render_spotify(spotify))
        if news_cfg.get("enabled", True):
            layout["news"].update(self.render_news(news))
        if self.pet:
            width = max(20, self.width - 1)
            layout["pet"].update(self.pet.render(width, music=spotify.status == "playing"))
        layout["footer"].update(self.render_footer(stats))
        return layout

    def render_news(self, n: NewsState) -> Panel:
        cfg = self.cfg["news"]
        color = "magenta"
        if not n.ok:
            return Panel(Align.center(Text(n.error, style="dim"), vertical="middle"),
                         title="📰 ニュース", border_style=color)

        per_page = cfg["lines"]
        pages = max(1, -(-len(n.items) // per_page))  # 切り上げ
        page = int(time.time() // cfg["rotate_sec"]) % pages
        shown = n.items[page * per_page:(page + 1) * per_page]

        t = Table.grid(expand=True, padding=(0, 1))
        t.add_column(width=5, style="dim")
        t.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        t.add_column(style="dim", justify="right", max_width=16, overflow="ellipsis", no_wrap=True)
        for item in shown:
            t.add_row(item.published.strftime("%H:%M") if item.published else "", item.title, item.source)

        sub = f"{page + 1}/{pages}  更新 {n.updated:%H:%M}" + (f"  ⚠ {n.error}" if n.error else "")
        return Panel(t, title="📰 ニュース", subtitle=Text(sub, style="dim"), border_style=color)

    def render_clock(self, accent: str) -> Panel:
        now = dt.datetime.now()
        # 幅が足りないとき（40文字未満）は秒を省いて 時:分 だけにする
        if self.cfg["display"].get("show_seconds", True) and getattr(self, "width", 80) >= 40:
            # 時:分 と 秒 で色を分けて、秒を少し控えめにする
            hm = big_text(now.strftime("%H:%M:")).split("\n")
            ss = big_text(now.strftime("%S")).split("\n")
            clock = Text()
            for i in range(5):
                clock.append(hm[i].ljust(max(len(r) for r in hm)) + " ", style=f"bold {accent}")
                clock.append(ss[i], style="bold white")
                if i < 4:
                    clock.append("\n")
        else:
            sep = ":" if now.second % 2 == 0 else " "  # 秒なしのときはコロンを点滅
            clock = Text(big_text(now.strftime("%H") + sep + now.strftime("%M")), style=f"bold {accent}")
        weekday = "月火水木金土日"[now.weekday()]
        date = Text(f"{now:%Y年%m月%d日}（{weekday}）", style="bold")
        return Panel(Align.center(Group(Align.center(clock), Align.center(date)), vertical="middle"),
                     border_style=accent)

    # ─── 3DS など小さい画面用 ───
    def render_compact(self, weather: WeatherState, spotify: SpotifyState, stats: dict, accent: str) -> Layout:
        height = getattr(self, "height", 30)
        # 3DS の上画面（約20行）でもペットが入るよう、低い画面では詰めて表示する
        tight = height < 26
        rows = [
            Layout(self.render_clock_tight(accent) if tight else self.render_clock(accent),
                   name="clock", size=6 if tight else 9),
            Layout(self.render_weather_compact(weather, accent, tight), name="weather", size=4 if tight else 5),
            Layout(self.render_spotify_compact(spotify, tight), name="spotify", size=5 if tight else 6),
        ]
        used = sum(r.size for r in rows)
        if self.pet and height - used >= 5:
            rows.append(Layout(self.pet.render(max(20, self.width - 1), music=spotify.status == "playing"),
                               name="pet", size=5))
            used += 5
        if height - used >= 1:
            rows.append(Layout(self.render_footer_compact(stats), name="footer", size=1))
        layout = Layout()
        layout.split_column(*rows)
        return layout

    def render_clock_tight(self, accent: str):
        """枠なし・余白なしの時計（6行）"""
        panel = self.render_clock(accent)
        return panel.renderable  # Panel の中身（中央寄せの時計＋日付）だけ使う

    def render_weather_compact(self, w: WeatherState, accent: str, tight: bool = False) -> Panel:
        city = self.cfg["weather"]["city"]
        if not w.ok:
            return Panel(Text(w.error, style="dim"), title=city, border_style="blue")
        _, desc = WMO_CODES.get(w.code, ("", f"code {w.code}"))
        line1 = Text.assemble((f"{desc} {w.temp:.1f}℃", f"bold {accent}"), (f"  体感{w.feels:.0f}℃", "dim"))
        line2 = Text.assemble((f"{w.t_max:.0f}℃", "red"), "/", (f"{w.t_min:.0f}℃", "blue"),
                              f"  降水{w.rain_prob}%  湿度{w.humidity}%")
        line3 = Text("  ".join(f"{h} {temp:.0f}℃ {p}%" for h, temp, p in w.hourly), style="dim",
                     overflow="ellipsis", no_wrap=True)
        if tight:  # 2行に圧縮
            line1 = Text.assemble((f"{desc} {w.temp:.1f}℃", f"bold {accent}"),
                                  (f" {w.t_max:.0f}", "red"), "/", (f"{w.t_min:.0f}℃", "blue"),
                                  f" 降水{w.rain_prob}%", overflow="ellipsis", no_wrap=True)
            return Panel(Group(line1, line3), title=city, border_style="blue")
        return Panel(Group(line1, line2, line3), title=city, border_style="blue")

    def render_spotify_compact(self, s: SpotifyState, tight: bool = False) -> Panel:
        green = "#1DB954"
        if s.status not in ("playing", "paused"):
            return Panel(Text(s.message or "接続中…", style="dim", overflow="ellipsis"),
                         title="Spotify", border_style=green)
        prog = s.live_progress_ms()

        def fmt(ms: int) -> str:
            sec = ms // 1000
            return f"{sec // 60}:{sec % 60:02d}"

        mark = "▶" if s.status == "playing" else "||"
        bar = ProgressBar(total=max(s.duration_ms, 1), completed=prog, width=None,
                          complete_style=green, finished_style=green)
        times = Table.grid(expand=True, padding=(0, 1))
        times.add_column(width=4, justify="right")
        times.add_column(ratio=1)
        times.add_column(width=4)
        times.add_row(Text(fmt(prog), style="dim"), bar, Text(fmt(s.duration_ms), style="dim"))
        body = Group(
            Text.assemble((mark + " ", f"bold {green}"), (s.title, "bold white"), overflow="ellipsis", no_wrap=True),
            Text(s.artists, style=green, overflow="ellipsis", no_wrap=True),
            *([] if tight else [Text("")]),
            times,
        )
        return Panel(body, title="Spotify", border_style=green)

    def render_footer_compact(self, st: dict) -> Text:
        parts = [(st.get("host", "?"), "bold")]
        if st.get("cpu_temp") is not None:
            c = st["cpu_temp"]
            parts.append((f" CPU{c:.0f}℃", "red" if c >= 70 else "yellow" if c >= 60 else "green"))
        if st.get("mem") is not None:
            parts.append((f" MEM{st['mem']:.0f}%", ""))
        parts.append(("  Ctrl+C:終了", "dim"))
        return Text.assemble(*parts, overflow="ellipsis", no_wrap=True)

    def render_calendar(self, accent: str) -> Panel:
        import calendar

        today = dt.date.today()
        holidays = {}
        try:
            import jpholiday  # 入っていれば祝日を赤くする
            holidays = dict(jpholiday.month_holidays(today.year, today.month))
        except Exception:
            pass

        t = Table.grid(padding=(0, 1))
        for _ in range(7):
            t.add_column(justify="right", width=2)
        t.add_row(*[Text(c, style=s) for c, s in zip("日月火水木金土", ["red"] + ["dim"] * 5 + ["blue"])])
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(today.year, today.month):
            cells = []
            for i, d in enumerate(week):
                if d.month != today.month:
                    cells.append(Text(""))
                    continue
                style = "red" if (i == 0 or d in holidays) else "blue" if i == 6 else ""
                if d == today:
                    style = f"bold black on {accent}"
                cells.append(Text(str(d.day), style=style))
            t.add_row(*cells)

        sub = ""
        upcoming = [(d, n) for d, n in holidays.items() if d >= today]
        if upcoming:
            d, n = min(upcoming)
            sub = f"{d.day}日 {n}"
        return Panel(Align.center(t, vertical="middle"), title=f"{today.year}年{today.month}月",
                     subtitle=Text(sub, style="dim red") if sub else None, border_style="dim")

    def render_today(self, w: WeatherState) -> Panel:
        now = dt.datetime.now()
        t = Table.grid(padding=(0, 1))
        t.add_column(style="dim", justify="right")
        t.add_column()

        if w.sunrise:
            t.add_row("日の出", Text(f"☀ {w.sunrise}", style="yellow"))
            t.add_row("日の入", Text(f"☾ {w.sunset}", style="#8aadf4"))

        # 月齢（2000/01/06 18:14 UTC の新月からの経過で計算）
        synodic = 29.530588853
        base = dt.datetime(2000, 1, 6, 18, 14, tzinfo=dt.timezone.utc)
        age = ((dt.datetime.now(dt.timezone.utc) - base).total_seconds() / 86400) % synodic
        moons = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
        names = ["新月", "三日月", "上弦", "十三夜", "満月", "寝待月", "下弦", "有明月"]
        idx = int((age / synodic) * 8 + 0.5) % 8
        t.add_row("月齢", f"{moons[idx]} {age:.1f} {names[idx]}")

        # 今年・今日の進み具合
        start = dt.datetime(now.year, 1, 1)
        year_pct = (now - start) / (dt.datetime(now.year + 1, 1, 1) - start) * 100
        day_pct = (now.hour * 3600 + now.minute * 60 + now.second) / 864
        t.add_row("今日", self._mini_bar(day_pct))
        t.add_row("今年", self._mini_bar(year_pct))
        week_no = now.isocalendar()[1]
        t.add_row("", Text(f"{now.timetuple().tm_yday}日目・{week_no}週", style="dim"))
        return Panel(Align.center(t, vertical="middle"), title="今日", border_style="dim")

    @staticmethod
    def _mini_bar(pct: float, width: int = 8) -> Text:
        filled = int(pct / 100 * width)
        return Text.assemble(("█" * filled, "green"), ("░" * (width - filled), "dim"), f" {pct:4.1f}%")

    def render_weather(self, w: WeatherState, accent: str) -> Panel:
        city = self.cfg["weather"]["city"]
        if not w.ok:
            return Panel(Align.center(Text(w.error, style="dim"), vertical="middle"),
                         title=f"天気 - {city}", border_style="blue")

        icon, desc = WMO_CODES.get(w.code, ("?", f"code {w.code}"))
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right", style="dim")
        t.add_column()
        t.add_row(Text(icon, style="bold"), Text(f"{desc}  {w.temp:.1f}℃", style=f"bold {accent}"))
        t.add_row("体感", f"{w.feels:.1f}℃")
        t.add_row("最高/最低", Text.assemble((f"{w.t_max:.0f}℃", "red"), " / ", (f"{w.t_min:.0f}℃", "blue")))
        t.add_row("降水確率", f"{w.rain_prob}%")
        t.add_row("湿度/風", f"{w.humidity}%  {w.wind:.1f}m/s")

        hourly = Table(box=None, show_header=True, header_style="dim", padding=(0, 1))
        for h, _, _ in w.hourly:
            hourly.add_column(h, justify="center")
        if w.hourly:
            hourly.add_row(*[f"{temp:.0f}℃" for _, temp, _ in w.hourly])
            hourly.add_row(*[Text(f"☂{p}%", style="blue" if p >= 50 else "dim") for _, _, p in w.hourly])

        sub = f"更新 {w.updated:%H:%M}" + (f"  ⚠ {w.error}" if w.error else "")
        return Panel(Group(t, Text(""), hourly), title=f"天気 - {city}",
                     subtitle=Text(sub, style="dim"), border_style="blue")

    def render_spotify(self, s: SpotifyState) -> Panel:
        green = "#1DB954"
        if s.status in ("init", "idle", "disabled", "no_auth", "error"):
            style = {"error": "red", "no_auth": "yellow"}.get(s.status, "dim")
            msg = s.message or "接続中…"
            return Panel(Align.center(Text(msg, style=style), vertical="middle"),
                         title="♫ Spotify", border_style=green)

        mark = "▶ 再生中" if s.status == "playing" else "⏸ 一時停止"
        prog = s.live_progress_ms()

        def fmt(ms: int) -> str:
            sec = ms // 1000
            return f"{sec // 60}:{sec % 60:02d}"

        bar = ProgressBar(total=max(s.duration_ms, 1), completed=prog, width=None,
                          complete_style=green, finished_style=green)
        times = Table.grid(expand=True, padding=(0, 1))
        times.add_column(justify="right", width=5)
        times.add_column(ratio=1)
        times.add_column(justify="left", width=5)
        times.add_row(Text(fmt(prog), style="dim"), bar, Text(fmt(s.duration_ms), style="dim"))

        flags = []
        if s.shuffle:
            flags.append("🔀 シャッフル")
        if s.repeat != "off":
            flags.append("🔁 " + ("1曲" if s.repeat == "track" else "リピート"))
        info = f"🔈 {s.device}" + (f"  {s.volume}%" if s.volume is not None else "")
        if flags:
            info += "   " + "  ".join(flags)

        body = Group(
            Text(mark, style=f"bold {green}" if s.status == "playing" else "yellow"),
            Text(""),
            Text(s.title, style="bold white", overflow="ellipsis", no_wrap=True),
            Text(s.artists, style=green, overflow="ellipsis", no_wrap=True),
            Text(s.album, style="dim", overflow="ellipsis", no_wrap=True),
            Text(""),
            times,
            Text(""),
            Text(info, style="dim", overflow="ellipsis", no_wrap=True),
        )
        return Panel(body, title="♫ Spotify", border_style=green)

    def render_footer(self, st: dict) -> Panel:
        parts = [("🍓 ", ""), (st.get("host", "?"), "bold")]
        if st.get("ip"):
            parts.append((f"  {st['ip']}", "dim"))
        if st.get("cpu_temp") is not None:
            c = st["cpu_temp"]
            parts.append((f"   CPU {c:.1f}℃", "red" if c >= 70 else "yellow" if c >= 60 else "green"))
        if st.get("load") is not None:
            parts.append((f"   負荷 {st['load']:.2f}", ""))
        if st.get("mem") is not None:
            parts.append((f"   メモリ {st['mem']:.0f}%", ""))
        if st.get("uptime"):
            parts.append((f"   稼働 {st['uptime']}", "dim"))
        parts.append(("   Ctrl+C で終了", "dim"))
        return Panel(Text.assemble(*parts), border_style="dim")


# ─────────────────────────────────────────────
# デモ用ダミーデータ
# ─────────────────────────────────────────────
def demo_weather() -> WeatherState:
    now = dt.datetime.now()
    return WeatherState(ok=True, error="", temp=14.3, feels=12.8, humidity=62, wind=3.4, code=2,
                        t_max=17, t_min=9, rain_prob=20,
                        hourly=[("06時", 11, 10), ("08時", 13, 20), ("10時", 15, 60)],
                        sunrise="06:15", sunset="16:33", updated=now)


def demo_news() -> NewsState:
    now = dt.datetime.now()
    titles = [f"サンプル見出し その{i}：ここにニュースのタイトルが入ります" for i in range(1, 9)]
    items = [NewsItem(t, "サンプル新聞", now - dt.timedelta(minutes=7 * i)) for i, t in enumerate(titles)]
    return NewsState(ok=True, error="", items=items, updated=now)


def demo_spotify() -> SpotifyState:
    return SpotifyState(status="playing", title="Sample Track Title", artists="Sample Artist",
                        album="Sample Album", device="MacBook Pro", volume=65, shuffle=True,
                        progress_ms=83_000, duration_ms=214_000, fetched_at=time.monotonic())


# ─────────────────────────────────────────────
# エントリポイント
# ─────────────────────────────────────────────
def run_auth(cfg: dict):
    sp = cfg["spotify"]
    if not sp["client_id"] or not sp["client_secret"]:
        sys.exit("config.toml の [spotify] に client_id と client_secret を書いてから実行してください。")
    oauth = make_spotify_oauth(cfg)
    print("下の URL を PC のブラウザで開いて Spotify にログイン・許可してください。")
    print("許可後、ブラウザが「接続できません」になったら、そのアドレスバーの URL を丸ごとコピーして貼り付けてください。")
    print("（ssh -L 8888:127.0.0.1:8888 で繋いでいれば自動で完了することもあります）\n")
    token = oauth.get_access_token(as_dict=False)  # open_browser=False なので URL を表示→入力待ち
    if token:
        print(f"\n認証完了。トークンを {SPOTIFY_CACHE.name} に保存しました。")


def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi ターミナルダッシュボード")
    ap.add_argument("--auth", action="store_true", help="Spotify の初回認証を行う")
    ap.add_argument("--demo", action="store_true", help="ダミーデータで表示だけ確認する")
    ap.add_argument("--once", action="store_true", help="1回だけ描画して終了（動作確認用）")
    ap.add_argument("--3ds", dest="compact", action="store_true",
                    help="3DS など小さい画面向けの簡易表示（時計・天気・Spotify・ペット）")
    args = ap.parse_args()

    cfg = load_config()
    if args.auth:
        run_auth(cfg)
        return

    console = Console()
    dash = Dashboard(cfg, demo=args.demo, compact=args.compact)
    dash.start()

    if args.once:
        time.sleep(1.5)
        console.print(dash.render(console.width, console.height), height=console.height)
        dash.stop.set()
        return

    fps = cfg["display"]["refresh_per_sec"]
    if args.compact:
        fps = min(fps, 2)  # 3DS の Wi-Fi・描画は遅いので更新回数を抑える
    try:
        with Live(dash.render(console.width, console.height), console=console, screen=True, refresh_per_second=fps) as live:
            while True:
                live.update(dash.render(console.width, console.height))
                time.sleep(1 / fps)
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop.set()


if __name__ == "__main__":
    main()
