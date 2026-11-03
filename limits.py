"""
Plan limits: the rate-limit windows that Claude Code's /usage screen shows. Which ones a
plan has is up to the server: the current 7-day week, per-model weeks, a 5-hour session on
plans that have one, and pay-as-you-go spend when it is on.

Nothing about these windows lands on disk. Claude Code fetches them from Anthropic's OAuth
usage endpoint with the sign-in token it keeps in the macOS Keychain, so this module does
the same fetch, read-only and opt-in (Settings → plan limits):
  • the token is read from the Keychain item "Claude Code-credentials" (or from the
    credentials file Claude Code uses where there is no Keychain). It is never refreshed,
    logged or written anywhere, and it reaches curl on stdin so it never shows in `ps`
  • the request goes through the system curl so TLS uses the macOS trust store; the
    bundled Python only knows Homebrew's certificate path, which other Macs lack
  • only percentages, reset times, surface names and the server's color reading are kept
    from the reply; the body is never logged
  • the panel names the plan from Claude Code's profile in ~/.claude.json; that file holds
    much else, so this takes the one key and logs nothing from it
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from collector import clean_text

log = logging.getLogger("agent_farm.limits")

SECURITY = "/usr/bin/security"
CURL = "/usr/bin/curl"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
GLOBAL_CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home())) / ".claude.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
HTTP_TIMEOUT_S = 15
KEYCHAIN_TIMEOUT_S = 120        # macOS may be showing the user an allow/deny dialog
MIN_INTERVAL_S = 15             # a refresh sooner than this after a good one is skipped

# A reply describes its windows two ways, and a server may send either or both.
# The older keys, in display order with their panel labels and window lengths. Per-model
# weeks are present only on plans that have them, and a key the plan lacks is null (Ultra has
# no five_hour). The reply gives only each window's reset time; the lengths are the ones the
# keys name, and scoped limits are weekly.
SESSION_S, WEEK_S = 5 * 3600, 7 * 86400
WINDOWS = (("five_hour", "session", SESSION_S), ("seven_day", "week", WEEK_S),
           ("seven_day_opus", "week · opus", WEEK_S), ("seven_day_sonnet", "week · sonnet", WEEK_S))
# The newer "limits" rows, classified by kind: the server's own list of the meters a plan has,
# so they win over a key for the same window. A kind not named here still gets a bar, labeled
# from its group, with no pace tick since its length is unknown.
KINDS = {"session": ("session", SESSION_S), "weekly_all": ("week", WEEK_S), "weekly_scoped": ("week", WEEK_S)}
LABEL_MAX = 20                  # a label from the server must leave room for its bar
# Pay-as-you-go spend, which has no clock. "extra_usage" has the switch; newer replies leave
# its utilization null and give the share in "spend".
EXTRA_KEY, EXTRA_LABEL, SPEND_KEY = "extra_usage", "extra", "spend"
EXPIRED = "sign-in expired; open Claude Code to refresh it"


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    used_pct: float                 # 0..100, as the API reports it
    resets_at: datetime | None      # timezone-aware, or None when the API gives none
    span_s: float = 0.0             # window length in seconds; 0 = no clock or an unknown length
    severity: str = ""              # the server's reading for the bar's color, when it gives one

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
    plan: str = ""                  # the plan's name, e.g. "ultra"
    fetched_at: float = 0.0         # time.time() of the last successful fetch, 0 = never
    error: str = ""                 # short, user-facing; set when the last refresh failed
    surfaces: tuple[tuple[str, float], ...] = ()    # (name, share of the week's use), largest first


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


def _plan_name(fallback: str) -> str:
    """The plan's name from the profile Claude Code keeps ("Ultra"). The sign-in only knows
    the subscription type it was issued under, which stays "max" for a renamed plan."""
    try:
        name = json.loads(GLOBAL_CONFIG.read_text())["oauthAccount"]["planDisplayName"]
    except (OSError, ValueError, KeyError, TypeError):
        return fallback
    return clean_text(name).lower() if isinstance(name, str) and name else fallback


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
    return replace(tok, plan=_plan_name(tok.plan)), ""


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


def _pct(value) -> float | None:
    """A percentage from the reply held to 0..100, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, min(100.0, float(value)))


def _key_windows(data: dict) -> list[Window]:
    """The windows the reply's older top-level keys describe."""
    out: list[Window] = []
    for key, label, span in WINDOWS:
        w = data.get(key)
        pct = _pct(w.get("utilization")) if isinstance(w, dict) else None
        if pct is not None:
            out.append(Window(key, label, pct, _parse_ts(w.get("resets_at")), span))
    return out


def _surfaces(block) -> tuple[tuple[str, float], ...]:
    """Where the week's use went, in the server's order, from "seven_day_breakdown". These
    are shares of what was used (they add up to 100), not of a limit. 0% is left out."""
    rows = block.get("rows") if isinstance(block, dict) else None
    out: list[tuple[str, float]] = []
    for r in rows if isinstance(rows, list) else ():
        if isinstance(r, dict) and isinstance(r.get("display_name"), str) and _pct(r.get("percent")):
            out.append((clean_text(r["display_name"]).lower()[:LABEL_MAX], _pct(r["percent"])))
    return tuple(out)


def _row_windows(rows) -> list[Window]:
    """The windows in the reply's "limits" list, in the server's order, one bar per label."""
    out: dict[str, Window] = {}
    if not isinstance(rows, list):
        return []
    for r in rows:
        pct = _pct(r.get("percent")) if isinstance(r, dict) else None
        if pct is None:
            continue
        kind, group = str(r.get("kind") or ""), str(r.get("group") or "")
        name = _scope_name(r.get("scope"))
        label, span = KINDS.get(kind, (group or kind, 0.0))
        if not label or (kind == "weekly_scoped" and not name):
            continue
        label = clean_text(f"{label} · {name.lower()}" if name else label)[:LABEL_MAX]
        severity = r.get("severity")
        out.setdefault(label, Window(f"{kind}:{name}", label, pct, _parse_ts(r.get("resets_at")), span,
                                     severity if isinstance(severity, str) else ""))
    return list(out.values())


def _extra_window(data: dict) -> Window | None:
    """Pay-as-you-go spend when the account has it on and a share of a limit can be told:
    an unlimited account has none."""
    extra, spend = data.get(EXTRA_KEY), data.get(SPEND_KEY)
    extra = extra if isinstance(extra, dict) else {}
    spend = spend if isinstance(spend, dict) else {}
    if not extra.get("is_enabled", spend.get("enabled")):
        return None
    pct = _pct(spend.get("percent"))
    if pct is None:
        pct = _pct(extra.get("utilization"))
    if pct is None:
        used, cap = extra.get("used_credits"), extra.get("monthly_limit")           # both in cents
        if _pct(used) is None or _pct(cap) is None or cap <= 0:
            return None
        pct = _pct(used / cap * 100)
    severity = spend.get("severity")
    return Window(EXTRA_KEY, EXTRA_LABEL, pct, None, severity=severity if isinstance(severity, str) else "")


def parse_report(data: dict, plan: str, now: float) -> LimitsReport:
    rows = _row_windows(data.get("limits"))
    shown = {w.label for w in rows}
    wins = [w for w in _key_windows(data) if w.label not in shown] + rows
    wins.sort(key=lambda w: {"session": 0, "week": 1}.get(w.label, 2))      # the rest keep their order
    extra = _extra_window(data)
    if extra is not None:
        wins.append(extra)
    if not wins:
        return LimitsReport((), plan, now, "no limit windows in the reply")
    return LimitsReport(tuple(wins), plan, now, "", _surfaces(data.get("seven_day_breakdown")))


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
