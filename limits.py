"""
Plan limits: the rate-limit windows that Claude Code's /usage screen shows. Which ones a
plan has is up to the server: the current 7-day week, per-model weeks, a 5-hour session on
plans that have one, and pay-as-you-go spend when it is on, with the credit balance behind it.

Nothing about these windows lands on disk. Claude Code fetches them from Anthropic's OAuth
endpoints with the sign-in token it keeps in the macOS Keychain, so this module does the same
fetches, read-only and opt-in (Settings → plan limits):
  • the token is read from the Keychain item "Claude Code-credentials", or the one Claude
    Code keeps for CLAUDE_CONFIG_DIR or CLAUDE_SECURESTORAGE_CONFIG_DIR when set, and failing
    that from the credentials file Claude Code falls back to. It is never refreshed, logged
    or written anywhere, and it reaches curl on stdin so it never shows in `ps`
  • the request goes through the system curl so TLS uses the macOS trust store; the
    bundled Python only knows Homebrew's certificate path, which other Macs lack
  • only percentages, reset times, surface names, the server's color reading and the credit
    balance are kept from the replies; the bodies are never logged
  • the panel names the plan from Claude Code's profile in ~/.claude.json, and the balance
    request needs the organization's ID from there; that file holds much else, so this takes
    those two keys and logs nothing from them
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pwd
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from collector import CLAUDE_DIR, clean_text

log = logging.getLogger("agent_farm.limits")

SECURITY = "/usr/bin/security"
CURL = "/usr/bin/curl"
KEYCHAIN_SERVICE = "Claude Code-credentials"
STORAGE_DIR_VAR = "CLAUDE_SECURESTORAGE_CONFIG_DIR"         # where Claude Code keeps the sign-in, if set
ACCOUNT_NAME = re.compile(r"[a-zA-Z0-9._-]+")               # the user names Claude Code files the item under
NO_ACCOUNT = "claude-code-user"                             # its stand-in for any other name
GLOBAL_CONFIG = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home())) / ".claude.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDITS_URL = "https://api.anthropic.com/api/oauth/organizations/{org}/prepaid/credits"
ORG_ID = re.compile(r"[0-9a-fA-F-]{36}")
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
WARN_PCT, OVER_PCT = 75, 90     # where a bar with no reading from the server turns orange, then red
EXPIRED = "sign-in expired; open Claude Code to refresh it"


@dataclass(frozen=True)
class Window:
    key: str
    label: str
    used_pct: float                 # 0..100, as the API reports it
    resets_at: datetime | None      # timezone-aware, or None when the API gives none
    span_s: float = 0.0             # window length in seconds; 0 = no clock or an unknown length
    severity: str = ""              # the server's reading for the bar's color, when it gives one
    money_left: str = ""            # the credit balance, on the spend bar
    credits_out: float | None = None    # where on the spend bar the credits run out, 0..1
    funded_pct: float | None = None     # the spend bar's percent: of the money there is to spend, not the cap

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
    org: str = ""                   # the organization's ID, for the credit balance


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


def _profile_value(key: str) -> str:
    """One string from the profile Claude Code keeps, or "" when it isn't there."""
    try:
        value = json.loads(GLOBAL_CONFIG.read_text())["oauthAccount"][key]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return value if isinstance(value, str) else ""


def _plan_name(fallback: str) -> str:
    """The plan's name from the profile ("Ultra"). The sign-in only knows the subscription
    type it was issued under, which stays "max" for a renamed plan."""
    name = _profile_value("planDisplayName")
    return clean_text(name).lower() if name else fallback


def _org_id() -> str:
    """The organization's ID from the profile. It goes into a request, so only that shape will do."""
    org = _profile_value("organizationUuid")
    return org if ORG_ID.fullmatch(org) else ""


def _keychain_service() -> str:
    """The Keychain item that holds the sign-in, named the way Claude Code (2.1.321) names it.
    A CLAUDE_CONFIG_DIR gets an item of its own, suffixed with the first 8 hex characters of
    the SHA-256 of the path exactly as set, Unicode NFC being the only change; even a dir that
    names the default one gets the suffix. CLAUDE_SECURESTORAGE_CONFIG_DIR, when set, decides
    instead: its value is hashed, and set but empty it means the plain item. Otherwise a custom
    dir never uses the plain item, which holds another account's sign-in."""
    store = os.environ.get(STORAGE_DIR_VAR)
    named = store if store is not None else os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not named:
        return KEYCHAIN_SERVICE
    key = unicodedata.normalize("NFC", named).encode()
    return f"{KEYCHAIN_SERVICE}-{hashlib.sha256(key).hexdigest()[:8]}"


def _keychain_account() -> str:
    """The account Claude Code (2.1.321) files its Keychain item under: $USER, else the login
    name, with its stand-in when the name has characters it doesn't accept."""
    try:
        name = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        name = NO_ACCOUNT
    return name if ACCOUNT_NAME.fullmatch(name) else NO_ACCOUNT


def _credentials_file() -> Path:
    """The file Claude Code falls back to when the Keychain fails: in
    CLAUDE_SECURESTORAGE_CONFIG_DIR when that is set (~/.claude when it is empty), else in the
    dir the sessions come from."""
    store = os.environ.get(STORAGE_DIR_VAR)
    if store is None:
        return CLAUDE_DIR / ".credentials.json"         # the config dir the sessions come from
    return Path(store or Path.home() / ".claude") / ".credentials.json"


CREDENTIALS_FILE = _credentials_file()


def _from_keychain() -> tuple[str | None, str]:
    """The sign-in's text from the Keychain, as (blob, reason) with exactly one set. The item
    is asked for by service and account, as Claude Code asks for it, so a copy filed under
    another account is never picked up."""
    cmd = [SECURITY, "find-generic-password", "-a", _keychain_account(), "-s", _keychain_service(), "-w"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "Keychain did not answer"
    except OSError:
        return None, "security tool missing"
    if r.returncode == 0:
        return r.stdout, ""
    log.info("plan limits: Keychain lookup failed (exit %d)", r.returncode)
    # 44 is errSecItemNotFound; anything else is most often the user declining the dialog
    if r.returncode == 44:
        return None, "no Claude Code sign-in in the Keychain"
    return None, "Keychain access declined"


def load_token() -> tuple[_Token | None, str]:
    """Claude Code's sign-in, looked for in its order: the Keychain, then the credentials file.
    Returns (token, reason) with exactly one of them set. Nothing about the token is logged."""
    blob, why = _from_keychain()
    source = "Keychain"
    if blob is None:
        if not CREDENTIALS_FILE.exists():
            return None, why
        try:
            blob = CREDENTIALS_FILE.read_text()
        except OSError:
            return None, "credentials file unreadable"
        source = "credentials file"
    tok = _parse_credentials(blob)
    if tok is None:
        return None, f"unexpected {source} format"
    return replace(tok, plan=_plan_name(tok.plan), org=_org_id()), ""


def _fetch_json(url: str, token: str, *headers: str) -> tuple[dict | None, str]:
    """GET a JSON reply through the system curl. The URL and token travel in a curl config
    on stdin, never in the argument list. Returns (reply, reason)."""
    config = (f'url = "{url}"\n'
              f'header = "Authorization: Bearer {token}"\n'
              f'header = "anthropic-beta: {OAUTH_BETA}"\n'
              'header = "Accept: application/json"\n'
              + "".join(f'header = "{h}"\n' for h in headers))
    cmd = [CURL, "--silent", "--show-error", "--config", "-", "--max-time", str(HTTP_TIMEOUT_S),
           "--write-out", "\n%{http_code}"]
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


def fetch_usage(token: str) -> tuple[dict | None, str]:
    return _fetch_json(USAGE_URL, token)


def fetch_credits(token: str, org: str) -> float | None:
    """The prepaid credit balance in cents, which the usage reply leaves out, or None when it
    can't be told. Dollars are the one currency this has seen, so no other is read as them."""
    data, _ = _fetch_json(CREDITS_URL.format(org=org), token, f"x-organization-uuid: {org}")
    if not data or data.get("currency") != "USD" or _pct(data.get("amount")) is None:
        return None
    return max(0.0, float(data["amount"]))


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


def _money(cents: float) -> str:
    """Dollars for the six characters beside a bar, so no cents from $100 up."""
    return f"${cents / 100:.2f}" if cents < 10000 else f"${cents / 100:.0f}"


def _credits_out(extra: dict, balance: float) -> float | None:
    """Where on the spend bar the credits run out: what has been spent plus what is left, on
    the monthly cap's scale, so it holds still as they are spent. None at or past the cap."""
    used, cap = extra.get("used_credits"), extra.get("monthly_limit")               # both in cents
    if _pct(used) is None or _pct(cap) is None or cap <= 0:
        return None
    out = (used + balance) / cap
    return out if out < 1 else None


def _funded_pct(extra: dict, balance: float) -> float | None:
    """How much of the money there is to spend has gone: what has been spent, of that plus
    what is left. It reaches 100 as the bar's fill reaches the credits tick. None with nothing
    spent or left, when there is no such share."""
    used = extra.get("used_credits")                                                # in cents
    if _pct(used) is None or used + balance <= 0:
        return None
    return _pct(used / (used + balance) * 100)


def _spend_severity(server: str, funded: float | None) -> str:
    """The spend bar's color reading. The server's is of the cap, so the money can run low
    while it still says normal: the worse of the two counts."""
    if funded is None:
        return server
    if funded >= OVER_PCT or server == "critical":
        return "critical"
    return "warning" if funded >= WARN_PCT or server == "warning" else server


def _extra_window(data: dict, balance: float | None = None) -> Window | None:
    """Pay-as-you-go spend when the account has it on and a share of a limit can be told:
    an unlimited account has none. `balance` is the credit balance in cents, when known."""
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
    funded = None if balance is None else _funded_pct(extra, balance)
    return Window(EXTRA_KEY, EXTRA_LABEL, pct, None,
                  severity=_spend_severity(severity if isinstance(severity, str) else "", funded),
                  money_left="" if balance is None else _money(balance),
                  credits_out=None if balance is None else _credits_out(extra, balance),
                  funded_pct=funded)


def parse_report(data: dict, plan: str, now: float, balance: float | None = None) -> LimitsReport:
    rows = _row_windows(data.get("limits"))
    shown = {w.label for w in rows}
    wins = [w for w in _key_windows(data) if w.label not in shown] + rows
    wins.sort(key=lambda w: {"session": 0, "week": 1}.get(w.label, 2))      # the rest keep their order
    extra = _extra_window(data, balance)
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
            # Without a balance the spend bar still reads, so a failure here is no error for the panel.
            balance = fetch_credits(tok.value, tok.org) if tok.org and _extra_window(data) else None
            return self._set(parse_report(data, tok.plan, now, balance))

    def _set(self, rep: LimitsReport) -> bool:
        changed = rep != self._report
        self._report = rep
        if changed and rep.error:
            log.info("plan limits: %s", rep.error)
        return changed
