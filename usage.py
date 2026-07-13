"""Data layer: read local Claude Code + Codex logs and compute usage.

No network. All numbers come from files already on this machine:
  Claude : ~/.claude/projects/**/*.jsonl   (per-message token usage; no rate-limit
           telemetry, so 5h / weekly percentages are ESTIMATES vs a configurable ceiling)
  Codex  : ~/.codex/sessions/**/*.jsonl + ~/.codex/archived_sessions/**/*.jsonl
           (token_count events carry an EXACT rate_limits block -> used_percent + resets_at)

Run standalone to sanity-check:  python usage.py
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
CLAUDE_ROOT = HOME / ".claude" / "projects"
CODEX_ROOTS = [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"]

# Claude Code's status-line hook receives the REAL rate-limit numbers (the same
# ones the /usage screen shows). statusline-command.sh dumps that payload here, so
# we can read exact percentages instead of estimating from token counts.
STATUSLINE_FILE = HOME / ".claude" / "last-statusline.json"

FIVE_HOURS = 5 * 3600
WEEK = 7 * 24 * 3600

# Fallback ceilings used only to turn Claude token counts into a rough percentage.
# Counted tokens = input + output + cache_creation (cache_read is excluded: it is
# heavily discounted and would otherwise dwarf everything). Override in config.json:
#   "claude_ceilings": {"5h": ..., "wk": ...}
DEFAULT_CLAUDE_CEILINGS = {"5h": 8_000_000, "wk": 50_000_000}

# Only look at files touched within this many days (keeps every refresh fast).
SCAN_DAYS = 9


def _now() -> float:
    return time.time()


def _parse_ts(s: str) -> float | None:
    """ISO-8601 (…Z) -> epoch seconds."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _recent_jsonl(root: Path, since: float):
    if not root.exists():
        return
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime >= since:
                yield path
        except OSError:
            continue


def _iter_lines(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield line
    except OSError:
        return


# --------------------------------------------------------------------------- Claude

def _claude_events(since: float):
    """Yield (epoch, tokens, model) for each assistant message since `since`."""
    for path in _recent_jsonl(CLAUDE_ROOT, since - 3600):
        for line in _iter_lines(path):
            if '"assistant"' not in line or '"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            ts = _parse_ts(obj.get("timestamp", ""))
            if ts is None or ts < since:
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage") or {}
            tokens = (
                int(u.get("input_tokens", 0) or 0)
                + int(u.get("output_tokens", 0) or 0)
                + int(u.get("cache_creation_input_tokens", 0) or 0)
            )
            if tokens:
                yield ts, tokens, msg.get("model")


def _current_5h_block(events: list[tuple[float, int, object]], now: float):
    """ccusage-style 5h session block. events sorted ascending by time.

    Returns (tokens_in_active_block, reset_at_epoch) or (0, None) if no active block.
    A block starts on the first message (floored to the hour) and lasts 5h; a gap of
    more than 5h since the last message also starts a fresh block.
    """
    start = last = None
    tokens = 0
    for ts, tok, _ in events:
        if start is None or ts >= start + FIVE_HOURS or ts - last >= FIVE_HOURS:
            start = (ts // 3600) * 3600  # floor to the hour
            last = ts
            tokens = tok
        else:
            last = ts
            tokens += tok
    if start is None or now >= start + FIVE_HOURS:
        return 0, None
    return tokens, start + FIVE_HOURS


def claude_live(now: float) -> dict | None:
    """Exact Claude usage from the status-line payload (None if unavailable)."""
    try:
        raw = STATUSLINE_FILE.read_text(encoding="utf-8")
        d = json.loads(raw)
        mtime = STATUSLINE_FILE.stat().st_mtime
    except (OSError, json.JSONDecodeError):
        return None
    rl = d.get("rate_limits") or {}
    five = rl.get("five_hour") or {}
    week = rl.get("seven_day") or {}
    if five.get("used_percentage") is None and week.get("used_percentage") is None:
        return None
    mo = d.get("model")
    model = mo.get("display_name") if isinstance(mo, dict) else None
    return {
        "ok": True,
        "source": "live",
        "model": model or "Claude",
        "tokens_5h": 0,
        "tokens_wk": 0,
        "pct_5h": float(five.get("used_percentage") or 0.0),
        "pct_wk": float(week.get("used_percentage") or 0.0),
        "reset_5h": five.get("resets_at"),
        "reset_wk": week.get("resets_at"),
        # fresh = Claude Code rendered its status line within the last hour
        "active": (now - mtime) < 3600,
        "updated": mtime,
    }


def claude_usage(ceilings: dict, now: float) -> dict:
    since = now - WEEK - 3600
    events = sorted(_claude_events(since), key=lambda e: e[0])
    tok_5h, reset_5h = _current_5h_block(events, now)
    tok_wk = sum(t for ts, t, _ in events if ts >= now - WEEK)
    model = events[-1][2] if events else None
    c5 = ceilings.get("5h") or DEFAULT_CLAUDE_CEILINGS["5h"]
    cw = ceilings.get("wk") or DEFAULT_CLAUDE_CEILINGS["wk"]
    return {
        "ok": True,
        "source": "estimate",
        "model": _pretty_model(model),
        "tokens_5h": tok_5h,
        "tokens_wk": tok_wk,
        "pct_5h": min(100.0, 100.0 * tok_5h / c5) if c5 else 0.0,
        "pct_wk": min(100.0, 100.0 * tok_wk / cw) if cw else 0.0,
        "reset_5h": reset_5h,
        "reset_wk": None,
        "active": reset_5h is not None,
    }


# --------------------------------------------------------------------------- Codex

def codex_usage(now: float) -> dict:
    since = now - WEEK - 3600
    best_ts = -1.0
    limits = None
    model = None
    tok_5h = tok_wk = 0
    for root in CODEX_ROOTS:
        for path in _recent_jsonl(root, since):
            for line in _iter_lines(path):
                if '"model"' in line and '"token_count"' not in line:
                    try:
                        o = json.loads(line)
                    except json.JSONDecodeError:
                        o = None
                    m = _find_key(o, "model") if o else None
                    if isinstance(m, str):
                        model = m
                if '"token_count"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(obj.get("timestamp", ""))
                info = (obj.get("payload") or {}).get("info") or {}
                last = info.get("last_token_usage") or {}
                delta = int(last.get("total_tokens", 0) or 0)
                if ts is not None and ts >= now - FIVE_HOURS:
                    tok_5h += delta
                if ts is not None and ts >= now - WEEK:
                    tok_wk += delta
                if ts is not None and ts > best_ts:
                    rl = (obj.get("payload") or {}).get("rate_limits")
                    if rl:
                        best_ts = ts
                        limits = rl

    result = {
        "ok": limits is not None,
        "source": "exact",
        "model": _pretty_model(model),
        "tokens_5h": tok_5h,
        "tokens_wk": tok_wk,
        "pct_5h": 0.0,
        "pct_wk": 0.0,
        "reset_5h": None,
        "reset_wk": None,
        "active": False,
        "plan": None,
    }
    if limits:
        prim = limits.get("primary") or {}
        sec = limits.get("secondary") or {}
        result["pct_5h"] = float(prim.get("used_percent", 0.0) or 0.0)
        result["pct_wk"] = float(sec.get("used_percent", 0.0) or 0.0)
        result["reset_5h"] = prim.get("resets_at")
        result["reset_wk"] = sec.get("resets_at")
        result["plan"] = limits.get("plan_type")
        result["active"] = now - best_ts < FIVE_HOURS
        # A snapshot whose reset time has already passed describes an elapsed window;
        # the live usage is effectively 0 until the next Codex activity refreshes it.
        if result["reset_5h"] and result["reset_5h"] <= now:
            result["pct_5h"] = 0.0
        if result["reset_wk"] and result["reset_wk"] <= now:
            result["pct_wk"] = 0.0
    return result


# --------------------------------------------------------------------------- helpers

def _find_key(obj, key):
    """Depth-first search for the first value of `key` in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _pretty_model(model: object) -> str | None:
    if not isinstance(model, str) or not model:
        return None
    m = model.lower()
    if "opus" in m:
        return "Opus " + _ver(m)
    if "sonnet" in m:
        return "Sonnet " + _ver(m)
    if "haiku" in m:
        return "Haiku " + _ver(m)
    if m.startswith("gpt-") or m.startswith("codex"):
        return model.replace("gpt-", "GPT-")
    return model


def _ver(m: str) -> str:
    import re
    hit = re.search(r"(\d+[-.]\d+|\d+)", m)
    return hit.group(1).replace("-", ".") if hit else ""


ROW_ORDER = ("5h", "Wk", "Fb")


def _row(label, pct, reset, est):
    return {"label": label, "pct": None if pct is None else float(pct),
            "reset": reset, "est": est, "placeholder": pct is None}


def _three_rows(found: dict) -> list:
    """Always return 5h / Wk / Fb in order; fill any missing one with a placeholder."""
    return [found.get(lbl) or _row(lbl, None, None, True) for lbl in ROW_ORDER]


def claude_rows(config: dict, now: float, force: bool = False) -> dict:
    """Always 3 rows (5h / Wk / Fb). Missing data shows as a "—" placeholder.
    Best source first: live API (all 3, exact) -> status line (5h/Wk) -> estimate (5h/Wk, ~).

    `force` (a manual logo click) is the only thing that actually calls the API;
    otherwise the API layer returns its last cached result with no network.
    """
    # 1. live API (same source Claude Code uses) — set "use_api": false to disable
    lim = None
    if config.get("use_api", True):
        try:
            import usage_api
            lim = usage_api.get_limits(force=force)
        except Exception:
            lim = None
    if lim:
        found, model = {}, None
        if "session" in lim:
            found["5h"] = _row("5h", lim["session"]["pct"], lim["session"]["reset"], False)
        if "weekly_all" in lim:
            found["Wk"] = _row("Wk", lim["weekly_all"]["pct"], lim["weekly_all"]["reset"], False)
        if "weekly_scoped" in lim:
            found["Fb"] = _row("Fb", lim["weekly_scoped"]["pct"], lim["weekly_scoped"]["reset"], False)
            model = lim["weekly_scoped"].get("model")
        if found:
            return {"ok": True, "source": "live-api", "model": model, "rows": _three_rows(found)}

    # 2. status-line file
    live = claude_live(now)
    if live:
        found = {
            "5h": _row("5h", live["pct_5h"], live["reset_5h"], False),
            "Wk": _row("Wk", live["pct_wk"], live["reset_wk"], False),
        }
        return {"ok": True, "source": "live", "model": live.get("model"), "rows": _three_rows(found)}

    # 3. token estimate
    est = claude_usage(config.get("claude_ceilings", {}), now)
    found = {
        "5h": _row("5h", est["pct_5h"], est["reset_5h"], True),
        "Wk": _row("Wk", est["pct_wk"], est["reset_wk"], True),
    }
    return {"ok": True, "source": "estimate", "model": est.get("model"), "rows": _three_rows(found)}


def get_usage(config: dict | None = None, force: bool = False) -> dict:
    """Top-level entry used by the UI. Never raises.

    `force=True` (manual logo click) is the only path that hits the network.
    """
    config = config or {}
    now = _now()
    try:
        claude = claude_rows(config, now, force=force)
    except Exception as exc:  # never let the bar crash on bad data
        claude = {"ok": False, "error": str(exc), "source": "estimate", "rows": []}
    return {"now": now, "claude": claude}


def _fmt_reset(epoch) -> str:
    if not epoch:
        return "-"
    rem = int(epoch - _now())
    if rem <= 0:
        return "now"
    h, m = divmod(rem // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


if __name__ == "__main__":
    data = get_usage()
    d = data["claude"]
    print(f"=== CLAUDE ({d.get('source')})  model={d.get('model')} ===")
    if not d.get("ok") or not d.get("rows"):
        print("  no data:", d.get("error", "none found"))
    for r in d.get("rows", []):
        tilde = "~" if r["est"] else " "
        print(f"  {r['label']:3s} {tilde}{r['pct']:5.1f}%   reset in {_fmt_reset(r['reset'])}")
