"""
Plan limits: the rate-limit windows that Claude Code's /usage screen shows (the current
5-hour session, the current 7-day week, and per-model weeks on plans that have them).

Nothing about these windows lands on disk. Claude Code fetches them from Anthropic's OAuth
usage endpoint with the sign-in token it keeps in the macOS Keychain, so this module does
the same fetch, read-only and opt-in (Settings → plan limits):
  • the token is read from the Keychain item "Claude Code-credentials" (or from the
    credentials file Claude Code uses where there is no Keychain). It is never refreshed,
    logged or written anywhere, and it reaches curl on stdin so it never shows in `ps`
  • the request goes through the system curl so TLS uses the macOS trust store; the
    bundled Python only knows Homebrew's certificate path, which other Macs lack
  • only percentages and reset times are kept from the reply; the body is never logged
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

log = logging.getLogger("agent_farm.limits")

SECURITY = "/usr/bin/security"
CURL = "/usr/bin/curl"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
HTTP_TIMEOUT_S = 15
KEYCHAIN_TIMEOUT_S = 120        # macOS may be showing the user an allow/deny dialog
MIN_INTERVAL_S = 15             # a refresh sooner than this after a good one is skipped

# Reply keys in display order with their panel labels and window lengths. Per-model weeks are
# present only on plans that have them; "extra" is pay-as-you-go spend when the account has
# it enabled, and has no clock. Model-scoped weeks such as Fable come from the reply's
# "limits" list (see _scoped_windows). The reply gives only each window's reset time; the
# lengths are the ones its keys name (five_hour, seven_day*), and scoped limits are weekly.
SESSION_S, WEEK_S = 5 * 3600, 7 * 86400
WINDOWS = (("five_hour", "session", SESSION_S), ("seven_day", "week", WEEK_S),
           ("seven_day_opus", "week · opus", WEEK_S), ("seven_day_sonnet", "week · sonnet", WEEK_S))
EXTRA_KEY, EXTRA_LABEL = "extra_usage", "extra"
EXPIRED = "sign-in expired; open Claude Code to refresh it"


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    used_pct: float                 # 0..100, as the API reports it
    resets_at: datetime | None      # timezone-aware, or None when the API gives none
    span_s: float = 0.0             # window length in seconds; 0 = no clock (extra spend)

    def elapsed(self, now: float) -> float | None:
        """How far through its window the clock is, 0..1, taking the window to end at
        resets_at. None without a clock, or once the window has reset since the reading."""
        if not self.span_s or self.resets_at is None:
            return None
        left = self.resets_at.timestamp() - now
        if left <= 0:
            return None
        return max(0.0, min(1.0, 1.0 - left / self.span_s))


@dataclass(frozen=True)
class LimitsReport:
    windows: tuple[Window, ...] = ()
    plan: str = ""                  # subscription type from the sign-in, e.g. "max"
    fetched_at: float = 0.0         # time.time() of the last successful fetch, 0 = never
    error: str = ""                 # short, user-facing; set when the last refresh failed


@dataclass(frozen=True)
class _Token:
    value: str
    expires_at: float               # epoch seconds; 0 = unknown
    plan: str


def _parse_credentials(blob: str) -> _Token | None:
    """The access token out of Claude Code's credentials JSON, or None if the shape is off."""
    try:
        data = json.loads(blob)
    except ValueError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str):
        return None
    exp = oauth.get("expiresAt")
    expires = float(exp) if isinstance(exp, (int, float)) else 0.0
    if expires > 1e11:              # Claude Code stores milliseconds
        expires /= 1000.0
    return _Token(oauth["accessToken"], expires, str(oauth.get("subscriptionType") or ""))


def load_token() -> tuple[_Token | None, str]:
    """Claude Code's sign-in: the credentials file when it exists, else the Keychain.
    Returns (token, reason) with exactly one of them set. Nothing about the token is logged."""
    if CREDENTIALS_FILE.exists():
        try:
            blob = CREDENTIALS_FILE.read_text()
        except OSError:
            return None, "credentials file unreadable"
        source = "credentials file"
    else:
        try:
            r = subprocess.run([SECURITY, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                               capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return None, "Keychain did not answer"
        except OSError:
            return None, "security tool missing"
        if r.returncode != 0:
            log.info("plan limits: Keychain lookup failed (exit %d)", r.returncode)
            # 44 is errSecItemNotFound; anything else is most often the user declining the dialog
            return None, ("no Claude Code sign-in in the Keychain" if r.returncode == 44
                          else "Keychain access declined")
        blob = r.stdout
        source = "Keychain"
    tok = _parse_credentials(blob)
    if tok is None:
        return None, f"unexpected {source} format"
    return tok, ""


def fetch_usage(token: str) -> tuple[dict | None, str]:
    """GET the usage endpoint through the system curl. The token travels in a curl config
    on stdin, never in the argument list. Returns (reply, reason)."""
    config = (f'header = "Authorization: Bearer {token}"\n'
              f'header = "anthropic-beta: {OAUTH_BETA}"\n'
              'header = "Accept: application/json"\n')
    cmd = [CURL, "--silent", "--show-error", "--config", "-", "--max-time", str(HTTP_TIMEOUT_S),
           "--write-out", "\n%{http_code}", USAGE_URL]
    try:
        r = subprocess.run(cmd, input=config, capture_output=True, text=True, timeout=HTTP_TIMEOUT_S + 5)
    except subprocess.TimeoutExpired:
        return None, "network timeout"
    except OSError:
        return None, "curl missing"
    if r.returncode != 0:
        log.info("plan limits: curl exit %d", r.returncode)       # the message could carry the URL; the code is enough
        return None, "network error"
    body, _, code = r.stdout.rpartition("\n")
    if code != "200":
        log.info("plan limits: HTTP %s", code)
        return None, EXPIRED if code == "401" else f"HTTP {code}"
    try:
        data = json.loads(body)
    except ValueError:
        return None, "unreadable reply"
    if not isinstance(data, dict):
        return None, "unreadable reply"
    log.debug("plan limits: reply keys %s", sorted(data))         # names only, never values
    return data, ""


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scope_name(scope) -> str:
    """The display name of a scoped limit: its model ("Fable"), else its surface."""
    if not isinstance(scope, dict):
        return ""
    for part in ("model", "surface"):
        v = scope.get(part)
        if isinstance(v, dict) and isinstance(v.get("display_name"), str) and v["display_name"]:
            return v["display_name"]
    return ""


def _scoped_windows(rows, taken: set[str]) -> list[Window]:
    """Weekly limits scoped to one model or surface, from the reply's newer "limits" list
    (where the Fable week lives). A scope already shown from a seven_day_* key is skipped."""
    out: list[Window] = []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict) or r.get("kind") != "weekly_scoped":
            continue
        name = _scope_name(r.get("scope"))
        pct = r.get("percent")
        if not name or not isinstance(pct, (int, float)):
            continue
        label = f"week · {name.lower()}"
        if label in taken:
            continue
        taken.add(label)
        out.append(Window(f"scoped:{name}", label, max(0.0, min(100.0, float(pct))),
                          _parse_ts(r.get("resets_at")), WEEK_S))
    return out


def parse_report(data: dict, plan: str, now: float) -> LimitsReport:
    wins: list[Window] = []
    for key, label, span in WINDOWS:
        w = data.get(key)
        if not isinstance(w, dict) or not isinstance(w.get("utilization"), (int, float)):
            continue
        pct = max(0.0, min(100.0, float(w["utilization"])))
        wins.append(Window(key, label, pct, _parse_ts(w.get("resets_at")), span))
    wins.extend(_scoped_windows(data.get("limits"), {w.label for w in wins}))
    extra = data.get(EXTRA_KEY)
    if isinstance(extra, dict) and extra.get("is_enabled") and isinstance(extra.get("utilization"), (int, float)):
        wins.append(Window(EXTRA_KEY, EXTRA_LABEL, max(0.0, min(100.0, float(extra["utilization"]))), None))
    if not wins:
        return LimitsReport((), plan, now, "no limit windows in the reply")
    return LimitsReport(tuple(wins), plan, now, "")


class PlanLimits:
    """Fetches on demand from a worker thread; report() is safe to call from the UI thread
    (reports are immutable and swapped whole). A failed refresh keeps the last good windows
    and adds the reason, so the panel shows stale bars plus a note rather than nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: _Token | None = None
        self._report = LimitsReport()

    def report(self) -> LimitsReport:
        return self._report

    def refresh(self, force: bool = False) -> bool:
        """One fetch. Returns True when the report changed."""
        with self._lock:
            now = time.time()
            cur = self._report
            if not force and not cur.error and now - cur.fetched_at < MIN_INTERVAL_S:
                return False
            tok = self._token
            if tok is None or (tok.expires_at and tok.expires_at <= now):
                tok, why = load_token()                # Claude Code may have refreshed it meanwhile
                if tok is None:
                    return self._set(replace(cur, error=why))
                self._token = tok
            if tok.expires_at and tok.expires_at <= now:
                return self._set(replace(cur, error=EXPIRED))
            data, why = fetch_usage(tok.value)
            if data is None:
                if why == EXPIRED:
                    self._token = None                 # re-read the sign-in next time
                return self._set(replace(cur, error=why))
            return self._set(parse_report(data, tok.plan, now))

    def _set(self, rep: LimitsReport) -> bool:
        changed = rep != self._report
        self._report = rep
        if changed and rep.error:
            log.info("plan limits: %s", rep.error)
        return changed
