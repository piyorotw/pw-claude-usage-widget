"""Live Claude usage from Anthropic's own OAuth usage endpoint.

Same source (and same token-refresh flow) as the reference macOS widget
(github.com/PanithanNanti/claude-usage-widget). It reads the Claude Code OAuth
token from `~/.claude/.credentials.json`, refreshes it when expired (writing the
new token back atomically, exactly as Claude Code does), then calls
`GET https://api.anthropic.com/api/oauth/usage`.

The token is used only as the Authorization header to Anthropic and is never
printed or logged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CRED_FILE = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public OAuth client id
USER_AGENT = "claude-cli/1.0 (external, statusbar-widget)"  # avoid WAF blocking bare urllib
HERE = Path(__file__).resolve().parent
_LOG = HERE / "statusbar.log"
_CACHE = HERE / ".usage_cache.json"

COOLDOWN_429 = 60.0  # after a 429, ignore further manual refreshes for this long


class _RateLimited(Exception):
    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after


def _log(msg: str):
    """Diagnostics only — NEVER receives the token."""
    try:
        with _LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%H:%M:%S} [api] {msg}\n")
    except OSError:
        pass


def _write_cred_blob(full: dict):
    """Persist the (refreshed) credentials, atomically, with a one-time backup."""
    bak = CRED_FILE.with_name(".credentials.json.bak")
    try:
        if not bak.exists():
            bak.write_text(CRED_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        pass
    tmp = CRED_FILE.with_name(".credentials.json.tmp")
    tmp.write_text(json.dumps(full), encoding="utf-8")
    os.replace(tmp, CRED_FILE)   # atomic on Windows + POSIX


def _refresh(full: dict, container: dict) -> bool:
    """Exchange the refresh token for a fresh access token and write it back.
    Returns True if the container now holds a fresh access token."""
    rt = container.get("refreshToken")
    if not isinstance(rt, str) or not rt:
        _log("cannot refresh: no refreshToken in credentials")
        return False
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        _log(f"token refresh HTTP {exc.code}: {detail!r}")
        return False
    except Exception as exc:
        _log(f"token refresh failed: {exc!r}")
        return False
    at = tok.get("access_token")
    if not isinstance(at, str) or not at:
        _log("token refresh: response had no access_token")
        return False
    container["accessToken"] = at
    if isinstance(tok.get("refresh_token"), str):
        container["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        container["expiresAt"] = int((time.time() + float(tok["expires_in"])) * 1000)
    try:
        _write_cred_blob(full)
    except OSError as exc:
        _log(f"refreshed token but could not write back: {exc!r}")
    _log("token refreshed OK")
    return True


def _access_token() -> str | None:
    if not CRED_FILE.exists():
        _log(f"no credentials file at {CRED_FILE}")
        return None
    try:
        full = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log(f"credentials unreadable: {exc!r}")
        return None
    # tokens live at the top level or under a nested key (e.g. "claudeAiOauth")
    container = None
    if isinstance(full, dict):
        if isinstance(full.get("accessToken"), str):
            container = full
        else:
            for v in full.values():
                if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                    container = v
                    break
    if container is None:
        _log("no accessToken field in credentials")
        return None

    # This only runs on a manual logo click, so just always mint a fresh token
    # rather than checking expiry — simpler and never serves a stale token.
    _refresh(full, container)   # updates container + writes back on success
    return container.get("accessToken")


def fetch_raw(timeout: float = 15.0):
    """Return the parsed JSON usage response, or None if unavailable."""
    tok = _access_token()
    if not tok:
        return None
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            # OAuth access tokens must be marked with this beta flag or the API
            # rejects them with 401 "invalid authentication credentials".
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            retry_after = None
            try:
                retry_after = float(hdrs.get("retry-after"))
            except (TypeError, ValueError):
                pass
            _log(f"HTTP 429. retry-after={hdrs.get('retry-after')} "
                 f"(will allow a retry in {int(retry_after) if retry_after else COOLDOWN_429}s)")
            raise _RateLimited(retry_after)
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:200]
        except Exception:
            pass
        _log(f"HTTP {exc.code} from usage endpoint. body={detail!r}")
        return None
    except Exception as exc:
        _log(f"request failed: {exc!r}")
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        _log(f"non-JSON response (first 120 chars)={body[:120]!r}")
        return None
    return data


def _parse_ts(s):
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        try:
            return float(s)
        except (ValueError, TypeError):
            return None


def _load_cache() -> dict:
    try:
        return json.loads(_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict):
    try:
        _CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


def get_limits(timeout: float = 15.0, force: bool = False) -> dict | None:
    """Usage limits. NEVER calls the network unless `force=True` (a manual refresh).

    Without `force` it just returns whatever was last cached (from the previous
    successful manual refresh) — so the widget never auto-polls the rate-limited
    endpoint. A forced refresh still respects a short cooldown after a 429 to avoid
    re-tripping the limit on rapid clicks.
    """
    import time
    now = time.time()
    cache = _load_cache()
    cached_limits = cache.get("limits") or None

    if not force:
        return cached_limits
    if now < cache.get("cooldown_until", 0):
        _log("manual refresh skipped (cooling down after a recent 429)")
        return cached_limits

    try:
        data = _fetch_and_parse(timeout)
    except _RateLimited as exc:
        # honour the server's Retry-After so the next click lands past the window
        wait = exc.retry_after if exc.retry_after else COOLDOWN_429
        cache["cooldown_until"] = now + wait + 3   # +3s safety margin
        _save_cache(cache)
        return cached_limits
    if data is None:                    # transient error -> keep serving cache
        return cached_limits

    _save_cache({"ts": now, "limits": data})
    return data


def _fetch_and_parse(timeout: float) -> dict | None:
    """One live API call, parsed into normalized limits (or None). May raise _RateLimited."""
    data = fetch_raw(timeout)
    if data is None:
        return None
    if not isinstance(data, dict):
        _log(f"unexpected response type: {type(data).__name__}")
        return None

    out: dict[str, dict] = {}
    limits = data.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            pct = item.get("percent", item.get("utilization"))
            if kind not in ("session", "weekly_all", "weekly_scoped") or pct is None:
                continue
            model = None
            scope = item.get("scope")
            if isinstance(scope, dict):
                m = scope.get("model")
                if isinstance(m, dict):
                    model = m.get("display_name")
            out[kind] = {
                "pct": float(pct),
                "reset": _parse_ts(item.get("resets_at")),
                "model": model,
            }
    else:  # fallback shape
        five = data.get("five_hour") or {}
        week = data.get("seven_day") or {}
        if five.get("utilization") is not None:
            out["session"] = {"pct": float(five["utilization"]),
                              "reset": _parse_ts(five.get("resets_at")), "model": None}
        if week.get("utilization") is not None:
            out["weekly_all"] = {"pct": float(week["utilization"]),
                                 "reset": _parse_ts(week.get("resets_at")), "model": None}
    if out:
        _log(f"ok: got limits {sorted(out.keys())}")
    else:
        _log(f"response had no usable limits (top-level keys={list(data.keys())})")
    return out or None


if __name__ == "__main__":
    lim = get_limits()
    if not lim:
        print("no data (token missing/expired, or endpoint unreachable)")
    else:
        for k in ("session", "weekly_all", "weekly_scoped"):
            if k in lim:
                d = lim[k]
                extra = f"  model={d['model']}" if d.get("model") else ""
                print(f"{k:14s} {d['pct']:5.1f}%  reset={d['reset']}{extra}")
