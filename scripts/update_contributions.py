#!/usr/bin/env python3
"""Refresh the Sonder9999 profile contribution GIFs from real GitHub data.

The two GIFs in ``assets/`` are the only generated artefacts:

* ``contrib-anim.gif``       -- light / comic fade-in theme
* ``contrib-night-anim.gif`` -- dark / meteor-shower theme

Data is read from the public contribution calendar HTML, i.e. no token or
secret is required.  The page contains one row per day (date, count, level);
private contributions are already included by GitHub as anonymous counts, so
the result matches the number shown on the GitHub profile exactly.

Requirements: ``pip install -r scripts/requirements.txt``
Usage:        ``python scripts/update_contributions.py``

The script is deterministic: fixed fonts, fixed random seeds and a pinned SVG
renderer.  Re-running it on the same data produces byte-identical GIFs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

import resvg_py
from PIL import Image, ImageColor, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATES = HERE / "templates"
FONT_DIR = HERE / "fonts"
ASSETS = ROOT / "assets"
README = ROOT / "README.md"
SNAPSHOT = ASSETS / "contributions.json"
GIF_DAY = ASSETS / "contrib-anim.gif"
GIF_NIGHT = ASSETS / "contrib-night-anim.gif"

FONT_FILES = [
    FONT_DIR / "NotoSans-Regular.ttf",
    FONT_DIR / "NotoSans-Bold.ttf",
    FONT_DIR / "NotoSans-ExtraBold.ttf",
    FONT_DIR / "JetBrainsMono-Regular.ttf",
    FONT_DIR / "JetBrainsMono-Bold.ttf",
]

# Card geometry (same as the original theme).
W, H = 880, 252
X0, Y0 = 58.0, 62.0
PITCH_Y = 13.8
CELL = 11.0
GRID_LEFT, GRID_RIGHT = 58.0, 834.0

FRAMES = 150
DAY_MS = 110
NIGHT_MS = 120
METEOR_FALL = 0.48  # seconds from upper-right to the cell centre

DAY_TARGETS = ["#e8e8e8", "#c9c9c9", "#969696", "#3b3b3b"]
NIGHT_TARGETS = ["#064E3B", "#047857", "#10B981", "#38BDF8"]

# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

def fetch_page(user: str, retries: int = 3) -> str:
    """Download the public contribution calendar fragment."""
    url = f"https://github.com/users/{user}/contributions"
    headers = {
        "User-Agent": "Sonder9999-profile-updater/1.0 (+https://github.com/Sonder9999/Sonder9999)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # network hiccup / rate limit
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(2 ** attempt)
    raise SystemExit(f"Could not fetch {url}: {last_error}")


def parse_contributions(html: str) -> list[dict]:
    """Return [{date, count, level}, ...] sorted by date.

    GitHub renders the calendar as a table; the count only appears in the
    tooltip text, so the tooltip and the <td> are joined by component id.
    """
    tip_re = re.compile(
        r'<tool-tip[^>]*for="(contribution-day-component-[\d-]+)"[^>]*>(.*?)</tool-tip>',
        re.S,
    )
    tips = {m.group(1): m.group(2) for m in tip_re.finditer(html)}

    days: list[dict] = []
    for m in re.finditer(r"<td\b([^>]*)>", html):
        attrs = m.group(1)
        date = re.search(r'data-date="([\d-]+)"', attrs)
        level = re.search(r'data-level="(\d)"', attrs)
        cid = re.search(r'id="(contribution-day-component-[\d-]+)"', attrs)
        if not (date and level and cid):
            continue
        text = tips.get(cid.group(1), "")
        count = re.search(r"(\d+) contribution", text)
        days.append(
            {
                "date": date.group(1),
                "count": int(count.group(1)) if count else 0,
                "level": int(level.group(1)),
            }
        )

    days.sort(key=lambda d: d["date"])
    if not (360 <= len(days) <= 375):
        raise SystemExit(f"Expected 360-375 days, got {len(days)}; GitHub markup may have changed")

    start = dt.date.fromisoformat(days[0]["date"])
    if start.weekday() != 6:  # GitHub calendars start on Sunday
        raise SystemExit(f"First calendar day {start} is not a Sunday")
    for i, day in enumerate(days):
        if dt.date.fromisoformat(day["date"]) != start + dt.timedelta(days=i):
            raise SystemExit(f"Non-contiguous calendar around {day['date']}")
        if not 0 <= day["level"] <= 4:
            raise SystemExit(f"Bad level {day['level']} on {day['date']}")

    total = sum(d["count"] for d in days)
    header = re.search(r'<h2[^>]*id="js-contribution-activity-description"[^>]*>\s*([\d,]+)\s*contributions', html)
    if header:
        shown = int(header.group(1).replace(",", ""))
        if shown != total:
            raise SystemExit(f"Header total {shown} != calendar sum {total}")
    if total <= 0:
        raise SystemExit("Calendar total is zero or empty")
    return days


def make_cells(days: list[dict]) -> tuple[list[dict], float]:
    start = dt.date.fromisoformat(days[0]["date"])
    ncols = (dt.date.fromisoformat(days[-1]["date"]) - start).days // 7 + 1
    if ncols < 52:
        raise SystemExit(f"Unexpected week count: {ncols}")
    pitch = (GRID_RIGHT - CELL - GRID_LEFT) / (ncols - 1)
    cells = []
    for d in days:
        delta = (dt.date.fromisoformat(d["date"]) - start).days
        c = dict(d)
        c["w"] = delta // 7
        c["d"] = delta % 7
        c["x"] = X0 + c["w"] * pitch
        c["y"] = Y0 + c["d"] * PITCH_Y
        c["cx"] = c["x"] + CELL / 2
        c["cy"] = c["y"] + CELL / 2
        cells.append(c)
    return cells, pitch


# ---------------------------------------------------------------------------
# SVG card
# ---------------------------------------------------------------------------

def build_svg(template: str, cells: list[dict], days: list[dict], pitch: float, night: bool) -> str:
    total = sum(d["count"] for d in days)
    active = sum(1 for d in days if d["count"] > 0)
    zero = len(days) - active
    daily_max = max(d["count"] for d in days)
    first, last = days[0]["date"], days[-1]["date"]
    start = dt.date.fromisoformat(first)

    # Month labels: first day of each month, skipping a partial first month.
    month_starts, seen = [], set()
    for d in days:
        day = dt.date.fromisoformat(d["date"])
        key = (day.year, day.month)
        if key not in seen:
            seen.add(key)
            month_starts.append(day)
    if month_starts and month_starts[0].day > 1:
        month_starts = month_starts[1:]

    placed, month_block = [], []
    for day in month_starts:
        week = (day - start).days // 7
        x = X0 + (week + 1) * pitch
        if x > 815 or (placed and x - placed[-1] < 26):
            continue
        placed.append(x)
        month_block.append(
            f'  <text x="{x:.1f}" y="52.0" font-family="JetBrains Mono" font-size="10.5" '
            f'font-weight="700" fill="#64748B">{day.strftime("%b")}</text>'
        )

    blank = "#111827" if night else "#F1F5F9"
    stroke = "#1E293B" if night else "#E2E8F0"
    cell_block = "".join(
        f'<rect x="{c["x"]:.1f}" y="{c["y"]:.1f}" width="11.0" height="11.0" rx="2.5" '
        f'fill="{blank}" stroke="{stroke}" stroke-width="1">'
        f'<title>{c["date"]}: {c["count"]} contributions</title></rect>'
        for c in cells
    )

    highlight = "#34D399" if night else "#0284C7"
    zero_colour = "#E2E8F0" if night else "#0F172A"
    footer = (
        f'<text x="24" y="226" font-family="JetBrains Mono" font-size="11" font-weight="600" '
        f'fill="#64748B">ACTIVE DAYS: <tspan fill="{highlight}" font-weight="700">{active}</tspan>'
        f" / {len(days)} \u00b7 ZERO DAYS: <tspan fill=\"{zero_colour}\" font-weight=\"700\">{zero}</tspan>"
        f' \u00b7 DAILY MAX: <tspan fill="{highlight}" font-weight="700">{daily_max}</tspan></text>'
    )

    return (
        template.replace("{{TOTAL}}", f"{total:,}")
        .replace("{{RANGE}}", f"{first} to {last}")
        .replace("{{MONTHS}}", "\n".join(month_block))
        .replace("{{CELLS}}", cell_block)
        .replace("{{FOOTER}}", footer)
    )


def render(svg: str) -> Image.Image:
    blob = resvg_py.svg_to_bytes(
        svg_string=svg,
        width=W,
        height=H,
        skip_system_fonts=True,
        font_files=[str(p) for p in FONT_FILES],
    )
    return Image.open(io.BytesIO(bytes(blob))).convert("RGBA")


# ---------------------------------------------------------------------------
# animation helpers
# ---------------------------------------------------------------------------

def rgba(colour, alpha: int = 255):
    if isinstance(colour, tuple):
        return colour if len(colour) == 4 else colour + (alpha,)
    r, g, b = ImageColor.getrgb(colour)
    return (r, g, b, alpha)


def mix(c1, c2, t: float):
    a = ImageColor.getrgb(c1) if isinstance(c1, str) else c1[:3]
    b = ImageColor.getrgb(c2) if isinstance(c2, str) else c2[:3]
    return tuple(round(a[i] * (1 - t) + b[i] * t) for i in range(3))


def smooth(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def schedule(cells: list[dict], seed: str, variant2: bool) -> list[dict]:
    active = [dict(c) for c in cells if c["count"] > 0]
    rng = random.Random(seed)
    if variant2:
        # First meteor hits an active cell near the top-right, then the rest
        # are shuffled so the shower covers the whole calendar.
        best = min(active, key=lambda c: ((52 - c["w"]) ** 2 * 0.2 + c["d"] ** 2))
        other = [c for c in active if c is not best]
        rng.shuffle(other)
        order = [best] + other
    else:
        for c in active:
            c["_k"] = (c["w"] // 6, rng.random(), (c["d"] % 2) * (-1) ** (c["w"] % 2))
        active.sort(key=lambda c: (c["_k"][0], c["_k"][1], c["_k"][2]))
        active.sort(key=lambda c: (0 if c["w"] > 39 and c["d"] < 3 else 1, c["_k"][0], c["_k"][1]))
        order = active
    start, finish = (1.15, 15.48) if variant2 else (0.9, 11.9)
    for i, c in enumerate(order):
        c["at"] = start + (finish - start) * i / max(1, len(order) - 1)
    return order


def draw_day(im: Image.Image, events: list[dict], t: float) -> None:
    d = ImageDraw.Draw(im, "RGBA")
    recent = []
    for c in events:
        age = t - c["at"]
        if age < 0:
            continue
        level = c["level"]
        stage = min(level, max(1, int(age / 0.12) + 1)) if age < 0.55 else level
        d.rounded_rectangle(
            (c["x"], c["y"], c["x"] + 11, c["y"] + 11),
            radius=1,
            fill=rgba(DAY_TARGETS[stage - 1], 255),
        )
        if age < 0.6:
            recent.append((c, age))

    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, "RGBA")
    for i in range(14):
        x = 52 + i * 6
        od.line((x, 58, x + 28, 48), fill=rgba("#000000", 24), width=1)
        od.line((x, 176, x + 28, 186), fill=rgba("#000000", 24), width=1)
    od.line((50, 74, 82, 60), fill=rgba("#000000", 36), width=2)
    od.line((800, 171, 828, 186), fill=rgba("#000000", 36), width=2)
    for c, age in recent[-10:]:
        f = max(0, 1 - age / 0.6)
        a = int(120 * f)
        od.rounded_rectangle(
            (c["x"] - 2, c["y"] - 2, c["x"] + 13, c["y"] + 13),
            radius=2,
            outline=rgba("#000000", a),
            width=1,
        )
        for dx, dy in ((-6, 0), (6, 0), (0, -6), (0, 6)):
            od.line(
                (c["cx"] + dx * 0.55, c["cy"] + dy * 0.55, c["cx"] + dx, c["cy"] + dy),
                fill=rgba("#111111", a),
                width=1,
            )
    im.alpha_composite(overlay)


def draw_night(im: Image.Image, events: list[dict], t: float) -> None:
    d = ImageDraw.Draw(im)
    for c in events:
        age = t - c["at"]
        if age < 0:
            continue
        sat = smooth(age / 0.32)
        colour = NIGHT_TARGETS[max(1, c["level"]) - 1]
        q = im.getpixel((int(c["x"] + 5), int(c["y"] + 5)))
        fill = mix(q, colour, sat)
        d.rounded_rectangle(
            (c["x"], c["y"], c["x"] + 10.5, c["y"] + 10.5), radius=2.4, fill=fill
        )
        if age < 0.33:
            opacity = max(0, 1 - age / 0.33)
            d.rounded_rectangle(
                (c["x"] + 0.6, c["y"] + 0.6, c["x"] + 9.8, c["y"] + 9.8),
                radius=2,
                fill=mix(fill, "#effbff", opacity * 0.42),
            )
    draw_meteors(im, events, t)


def draw_meteors(im: Image.Image, events: list[dict], t: float) -> None:
    """One meteor per active cell; the cell lights exactly when it lands."""
    art = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(art)
    primary, secondary, pale = "#6ce4fa", "#48a7ff", "#effbff"
    for c in events:
        x, y = c["cx"], c["cy"]
        age = t - c["at"]
        if -METEOR_FALL <= age < 0:
            p = smooth((age + METEOR_FALL) / METEOR_FALL)
            sx, sy = x + 34.0, y - 39.0
            # Keep the start below the header and inside the right edge.
            if sy < 42.0:
                k = (y - 42.0) / 39.0
                sx, sy = x + 34.0 * k, 42.0
            if sx > 856.0:
                k = (856.0 - x) / 34.0
                sx, sy = 856.0, y - 39.0 * k
            hx, hy = sx + (x - sx) * p, sy + (y - sy) * p
            dist = math.hypot(x - sx, y - sy) or 1.0
            ux, uy = (x - sx) / dist, (y - sy) / dist
            tail = min(30.0, dist * 0.8)
            tx, ty = hx - ux * tail, hy - uy * tail
            for width, colour in ((6, rgba(primary, 26)), (3, rgba(secondary, 80)), (1, rgba(pale, 235))):
                d.line((tx, ty, hx, hy), fill=colour, width=width)
            d.ellipse((hx - 5, hy - 5, hx + 5, hy + 5), fill=rgba(primary, 28))
            d.ellipse((hx - 2.6, hy - 2.6, hx + 2.6, hy + 2.6), fill=rgba(pale, 255))
        elif 0 <= age <= 0.62:
            v = max(0.0, 1 - age / 0.62)
            radius = 3 + age * 27
            d.ellipse((x - radius, y - radius, x + radius, y + radius),
                      outline=rgba(primary, round(200 * v)), width=2)
            if age < 0.23:
                alpha = round(230 * (1 - age / 0.23))
                d.line((x - 9 * v, y, x + 9 * v, y), fill=rgba(pale, alpha), width=2)
                d.line((x, y - 9 * v, x, y + 9 * v), fill=rgba(pale, alpha), width=2)
            for j in range(5):
                angle = j * math.tau / 5 + c["w"]
                px, py = x + radius * math.cos(angle), y + radius * math.sin(angle)
                rr = 1.3 + v * 1.5
                d.ellipse((px - rr, py - rr, px + rr, py + rr), fill=rgba(primary, round(195 * v)))
    im.alpha_composite(art)


def quantize_cube(frames: list[Image.Image]) -> list[Image.Image]:
    palette = []
    for r in range(6):
        for g in range(6):
            for b in range(6):
                palette.extend([r * 51, g * 51, b * 51])
    for k in range(24):
        palette.extend([round(k * 255 / 23)] * 3)
    palette = palette[:768] + [0] * max(0, 768 - len(palette))
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(palette)
    return [f.quantize(palette=pal_img, dither=Image.Dither.NONE) for f in frames]


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------

def save_gif(frames: list[Image.Image], path: Path, duration: int, optimize: bool) -> bool:
    tmp = path.with_name(path.name + ".tmp")
    frames[0].save(
        tmp,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        optimize=optimize,
        disposal=2,
    )
    new = tmp.read_bytes()
    old = path.read_bytes() if path.exists() else None
    if old == new:
        tmp.unlink()
        return False
    tmp.replace(path)
    return True


def save_text(path: Path, text: str) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def snapshot_payload(days: list[dict], user: str) -> dict:
    return {
        "user": user,
        "range": [days[0]["date"], days[-1]["date"]],
        "total": sum(d["count"] for d in days),
        "active_days": sum(1 for d in days if d["count"] > 0),
        "zero_days": sum(1 for d in days if d["count"] == 0),
        "daily_max": max(d["count"] for d in days),
        "days": {d["date"]: d["count"] for d in days},
    }


def update_readme(total: int) -> bool:
    text = README.read_text(encoding="utf-8")
    new = re.sub(
        r'alt="[\d,]+ contributions in the last year"',
        f'alt="{total:,} contributions in the last year"',
        text,
    )
    return new != text and save_text(README, new)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="Sonder9999", help="GitHub login to read")
    parser.add_argument("--dry-run", action="store_true", help="fetch and render, but write nothing")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    html = fetch_page(args.user)
    days = parse_contributions(html)
    cells, pitch = make_cells(days)
    total = sum(d["count"] for d in days)
    active = sum(1 for d in days if d["count"] > 0)
    daily_max = max(d["count"] for d in days)

    if not args.quiet:
        print(
            f"{args.user}: {len(days)} days ({days[0]['date']} .. {days[-1]['date']}), "
            f"{total:,} contributions, {active} active, {len(days) - active} zero, max {daily_max}"
        )

    day_svg = build_svg((TEMPLATES / "contrib-day.svg").read_text(encoding="utf-8"), cells, days, pitch, night=False)
    night_svg = build_svg((TEMPLATES / "contrib-night.svg").read_text(encoding="utf-8"), cells, days, pitch, night=True)
    day_base = render(day_svg)
    night_base = render(night_svg)

    day_events = schedule(cells, "sonder9999/day/v1", variant2=False)
    night_events = schedule(cells, "sonder9999/night/v1", variant2=True)

    day_frames = []
    for i in range(FRAMES):
        t = 15.0 if i >= FRAMES - 15 else 13.0 * i / (FRAMES - 15 - 1)
        im = day_base.copy()
        draw_day(im, day_events, t)
        day_frames.append(im.convert("P", palette=Image.ADAPTIVE, colors=255))

    night_frames = []
    for i in range(FRAMES):
        im = night_base.copy()
        draw_night(im, night_events, i * NIGHT_MS / 1000)
        night_frames.append(im.convert("RGB"))
    night_frames = quantize_cube(night_frames)

    if args.dry_run:
        if not args.quiet:
            print("dry-run: no files written")
        return

    changed_day = save_gif(day_frames, GIF_DAY, DAY_MS, optimize=False)
    changed_night = save_gif(night_frames, GIF_NIGHT, NIGHT_MS, optimize=True)
    changed_snapshot = save_text(
        SNAPSHOT,
        json.dumps(snapshot_payload(days, args.user), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    changed_readme = update_readme(total)

    if not args.quiet:
        state = lambda changed: "updated" if changed else "unchanged"
        print(f"day GIF:   {GIF_DAY.stat().st_size / 1024:.0f} KiB ({state(changed_day)})")
        print(f"night GIF: {GIF_NIGHT.stat().st_size / 1024:.0f} KiB ({state(changed_night)})")
        print(f"snapshot:  {state(changed_snapshot)}")
        print(f"README:    {state(changed_readme)}")


if __name__ == "__main__":
    main()
