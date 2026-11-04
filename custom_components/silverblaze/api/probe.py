"""``silverblaze-probe``: check a utility's compatibility and write a redacted report.

Runs on the contributor's own machine with their own login. Credentials are prompted
interactively, sent only to the utility's login hosts and never written anywhere. The
report is built from a positive key schema (see ``build_report``): anything not listed
there cannot reach the file, and every string is redacted on the way in.

No Home Assistant imports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date
from getpass import getpass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .allowlist import SILVERBLAZE_HOST, build_allowlist
from .client import DATA_URL, GET_PAGE_PARAMETERS_URL, META_URL, SilverblazeClient, hourly_series
from .discovery import (
    COMPATIBLE,
    DIRECT_LOGIN,
    INCOMPATIBLE,
    PARTIAL,
    SSO_HANDOFF,
    UNKNOWN_STYLE,
    DiscoveryPolicy,
    run_discovery,
    zone_for_label,
)
from .exceptions import EndpointNotAllowed, SilverblazeError, UnexpectedResponse
from .http import SilverblazeHttp
from .models import PageContext
from .redact import host_path, redact_text
from .utilities import UtilityModule, get_utilities, get_utility

PROBE_VERSION = "0.1.0"

_MAX_TEXT = 200
# Literal secrets shorter than this are not replaced inside free text: a 1-3 character
# username would corrupt every word. Pattern rules still apply to such text.
_MIN_SECRET = 4

# Fixed label values; anything else is written as "unknown".
MODES = frozenset({"known", "discovery"})
LOGIN_STYLES = frozenset({SSO_HANDOFF, DIRECT_LOGIN, UNKNOWN_STYLE})
VERDICTS = frozenset({COMPATIBLE, PARTIAL, INCOMPATIBLE})
STEP_NAMES = frozenset(
    {
        "login",
        "page_parameters",
        "metadata",
        "hourly_sample",
        # generic discovery
        "start_page",
        "form_post",
        "handoff",
    }
)
_MANIFEST = Path(__file__).resolve().parent.parent / "manifest.json"

# Extra keyword arguments for ``SilverblazeHttp`` (tests inject a fake clock here).
_HTTP_KWARGS: dict[str, Any] = {}

_EXCLUDED = "credentials, tokens, account or meter IDs, or usage values"


# --- report writer -------------------------------------------------------------------


def library_version() -> str:
    """Integration version from ``manifest.json``."""
    try:
        version = json.loads(_MANIFEST.read_text(encoding="utf-8")).get("version")
    except OSError, ValueError:
        return "unknown"
    return version if isinstance(version, str) else "unknown"


def _clean(value: Any, secrets: Iterable[str], limit: int = _MAX_TEXT) -> str:
    """Redact one free-text string: known secrets (4+ chars) verbatim, then the pattern rules."""
    text = str(value)
    for secret in secrets:
        if len(secret) >= _MIN_SECRET:
            text = text.replace(secret, "<redacted>")
    return redact_text(text)[:limit]


# A ``host.tld/seg/seg`` run inside a vetted exception message (e.g. a redirect target).
_HOST_PATH = re.compile(r"(?<![\w@.-])((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})((?:/[^\s/?#\"'<>]*)+)")
# Path segments kept verbatim: letters, hyphen and underscore only. A segment with a digit
# or a dot (an account number, ``jane.doe``) looks personal and becomes ``<redacted>``;
# the host is always kept.
_SAFE_SEGMENT = re.compile(r"[A-Za-z_-]+")


def _clean_host_paths(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        segments = [
            seg if (not seg or _SAFE_SEGMENT.fullmatch(seg)) else "<redacted>"
            for seg in match.group(2).split("/")
        ]
        return match.group(1) + "/".join(segments)

    return _HOST_PATH.sub(repl, text)


def _label(value: Any, allowed: frozenset[str]) -> str:
    """A fixed label field: only a known value passes, anything else is ``unknown``."""
    return value if isinstance(value, str) and value in allowed else "unknown"


def _clean_at(value: Any, secrets: Iterable[str]) -> str:
    """Reduce a URL or ``host/path`` to ``host/path``, redacting each path segment."""
    text = str(value)
    if "://" in text:
        text = host_path(text)
    text = text.split("?", 1)[0].split("#", 1)[0]
    return "/".join(_clean(part, secrets, 80) for part in text.split("/"))[:_MAX_TEXT]


def _num(value: Any, kind: type) -> Any:
    """Return ``value`` if it is a real int/float (not bool), else None."""
    if isinstance(value, bool) or not isinstance(value, kind):
        return None
    return value


def _clean_step(step: Mapping[str, Any], secrets: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {"step": _label(step.get("step"), STEP_NAMES)}
    status = step.get("status")
    if _num(status, int) is not None or status in ("ok", "failed"):
        out["status"] = status
    if "at" in step:
        out["at"] = _clean_at(step["at"], secrets)
    names = step.get("field_names")
    if isinstance(names, (list, tuple, set, frozenset)):
        out["field_names"] = sorted(_clean(n, secrets, 64) for n in names)
    if _num(step.get("client_id"), int) is not None:
        out["client_id"] = step["client_id"]
    if isinstance(step.get("tab_key"), str):
        out["tab_key"] = _clean(step["tab_key"], secrets, 64)
    seconds = step.get("seconds")
    if _num(seconds, (int, float)) is not None:
        out["seconds"] = round(float(seconds), 1)
    if "error" in step and step["error"] is not None:
        out["error"] = _clean(_clean_host_paths(str(step["error"])), secrets)
    return out


def _clean_date(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def build_report(raw: Mapping[str, Any], secrets: Iterable[str] = ()) -> dict[str, Any]:
    """Build the report from a positive key schema; nothing else is ever copied.

    ``secrets`` are literal strings (username, password, ``enc``, IDs ...) that are
    removed from every string wherever they appear, on top of the pattern rules.
    """
    known = tuple(s for s in secrets if isinstance(s, str) and s)
    report: dict[str, Any] = {
        "probe_version": PROBE_VERSION,
        "library_version": library_version(),
        "generated_at": _clean_date(raw.get("generated_at")),
        "mode": _label(raw.get("mode"), MODES),
        "login_style": _label(raw.get("login_style", UNKNOWN_STYLE), LOGIN_STYLES),
        "verdict": _label(raw.get("verdict", INCOMPATIBLE), VERDICTS),
        "failed_step": None
        if raw.get("failed_step") is None
        else _label(raw["failed_step"], STEP_NAMES),
        "steps": [_clean_step(s, known) for s in raw.get("steps", []) if isinstance(s, Mapping)],
        "series": [
            {
                key: _clean(item.get(key, ""), known, 32)
                for key in ("interval", "kind", "uom", "commodity")
            }
            for item in raw.get("series", [])
            if isinstance(item, Mapping)
        ],
    }
    span = raw.get("ami_span")
    if isinstance(span, Mapping):
        report["ami_span"] = {
            "oldest_date": _clean_date(span.get("oldest_date")),
            "newest_date": _clean_date(span.get("newest_date")),
        }
    sample = raw.get("sample_day")
    if isinstance(sample, Mapping):
        report["sample_day"] = {
            "slots": _num(sample.get("slots"), int),
            "non_null": _num(sample.get("non_null"), int),
        }
    return report


def write_report(report: dict[str, Any], directory: Path | None = None) -> Path:
    """Write ``probe-report-<date>-<mode>.json`` (git-ignored) and return its path.

    An existing report is never overwritten: a later one gets ``-2``, ``-3`` ... before
    ``.json``. The mode suffix is omitted when the report has no known mode.
    """
    stamp = report.get("generated_at") or date.today().isoformat()
    mode = report.get("mode")
    # Only the two fixed mode labels ever reach the file name.
    known_mode = isinstance(mode, str) and mode in MODES
    base = f"probe-report-{stamp}" + (f"-{mode}" if known_mode else "")
    folder = directory or Path.cwd()
    text = json.dumps(report, indent=2, sort_keys=False) + "\n"
    n = 1
    while True:
        path = folder / (f"{base}.json" if n == 1 else f"{base}-{n}.json")
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            n += 1
            continue
        return path


# --- known-utility flow ----------------------------------------------------------------


def _login_style(module: type[UtilityModule]) -> str:
    hosts = {host.lower() for host, _ in module.login_endpoints()}
    return SSO_HANDOFF if hosts - {SILVERBLAZE_HOST} else DIRECT_LOGIN


class _Run:
    """Accumulates raw step data and prints the terminal lines."""

    def __init__(self, raw: dict[str, Any], secrets: list[str]) -> None:
        self.raw = raw
        self.secrets = secrets
        self._n = 0

    def line(self, label: str, status: str, detail: str = "") -> None:
        self._n += 1
        print(f" [{self._n}] {label} ".ljust(44, ".") + f" {status:<4} {detail}".rstrip())

    async def step(
        self, name: str, label: str, action: Callable[[], Awaitable[dict[str, Any]]], **base: Any
    ) -> dict[str, Any] | None:
        """Time ``action``; record an ok step, or a failed one (and return None)."""
        record: dict[str, Any] = {"step": name, **base}
        started = time.monotonic()
        try:
            extra = await action()
        except Exception as err:  # the probe must always produce a report
            record["seconds"] = time.monotonic() - started
            record["status"] = "failed"
            # Only our own exceptions carry fixed, vetted text; anything else (a parser or
            # library error) may embed response data, so only its class name is kept.
            vetted = isinstance(err, (SilverblazeError, EndpointNotAllowed))
            record["error"] = f"{type(err).__name__}: {err}" if vetted else type(err).__name__
            self.raw["steps"].append(record)
            self.raw["failed_step"] = name
            self.line(label, "FAIL", type(err).__name__)
            return None
        record["seconds"] = time.monotonic() - started
        record["status"] = "ok"
        detail = extra.pop("_detail", "")
        record.update(extra)
        self.raw["steps"].append(record)
        self.line(label, "ok", detail)
        return extra


def _local_date(value: Any, tz: ZoneInfo) -> date:
    return value.astimezone(tz).date()


async def _data_steps(
    http: SilverblazeHttp,
    run: _Run,
    raw: dict[str, Any],
    secrets: list[str],
    enc: str,
    tz: ZoneInfo | None,
) -> None:
    """The shared Silverblaze calls: page parameters, metadata, newest-day sample.

    ``tz`` is the utility's zone, or None to derive it from the service point's label.
    """
    ctx_box: dict[str, PageContext] = {}
    meta_box: dict[str, Any] = {}
    client = SilverblazeClient(http)

    async def page_parameters() -> dict[str, Any]:
        ctx = await client.get_page_parameters(enc)
        secrets.append(ctx.parameters_b64)
        ctx_box["ctx"] = ctx
        return {
            "client_id": ctx.client_id,
            "tab_key": ctx.tab_key,
            "_detail": f"ClientId {ctx.client_id}, tab {ctx.tab_key}",
        }

    async def metadata() -> dict[str, Any]:
        points = await client.get_meta(ctx_box["ctx"])
        secrets.extend(sp.id for sp in points)
        raw["series"] = [
            {
                "interval": s.interval,
                "kind": s.kind,
                "uom": s.uom,
                "commodity": s.commodity_code,
            }
            for p in points
            for s in p.series
        ]
        sp = next((p for p in points if hourly_series(p) is not None), None)
        series = hourly_series(sp) if sp is not None else None
        if sp is None or series is None:
            raise UnexpectedResponse("Meta response has no hourly kWh usage series")
        zone = tz or ZoneInfo(zone_for_label(sp.timezone_label))
        meta_box.update(sp=sp, series=series, tz=zone)
        raw["ami_span"] = {
            "oldest_date": _local_date(series.oldest_utc, zone),
            "newest_date": _local_date(series.newest_utc, zone),
        }
        span = raw["ami_span"]
        return {
            "_detail": (
                f"hourly series {series.interval}/{series.kind}/{series.uom}/"
                f"{series.commodity_code}; AMI {span['oldest_date']} to {span['newest_date']}"
            )
        }

    async def sample() -> dict[str, Any]:
        newest: date = raw["ami_span"]["newest_date"]
        day = await client.get_day(
            ctx_box["ctx"], meta_box["sp"], meta_box["series"], newest, meta_box["tz"]
        )
        raw["sample_day"] = {"slots": day.slot_count, "non_null": len(day.reads)}
        return {"_detail": f"{day.slot_count} slots, {len(day.reads)} non-null"}

    steps: list[tuple[str, str, Callable[[], Awaitable[dict[str, Any]]], dict[str, Any]]] = [
        (
            "page_parameters",
            "page parameters",
            page_parameters,
            {"at": host_path(GET_PAGE_PARAMETERS_URL)},
        ),
        ("metadata", "metadata", metadata, {"at": host_path(META_URL)}),
        ("hourly_sample", "hourly sample (newest day)", sample, {"at": host_path(DATA_URL)}),
    ]
    for name, label, action, base in steps:
        if await run.step(name, label, action, **base) is None:
            break


async def _discovery_data_steps(
    http: SilverblazeHttp, raw: dict[str, Any], secrets: list[str], enc: str
) -> None:
    run = _Run(raw, secrets)
    run._n = len(raw["steps"])
    await _data_steps(http, run, raw, secrets, enc, None)


async def run_known(
    http: SilverblazeHttp, module: type[UtilityModule], username: str, password: str
) -> tuple[dict[str, Any], list[str]]:
    """Run the known-utility flow; return the raw report data and discovered secrets."""
    secrets = [username, password]
    raw: dict[str, Any] = {
        "mode": "known",
        "login_style": _login_style(module),
        "steps": [],
        "series": [],
        "failed_step": None,
    }
    run = _Run(raw, secrets)
    tz = ZoneInfo(module.timezone())

    async def login() -> dict[str, Any]:
        enc = await module.login(http, username, password)
        secrets.append(enc)
        return {"_detail": f"login style: {raw['login_style']}"}

    login_hosts = sorted({h.lower() for h, _ in module.login_endpoints()} - {SILVERBLAZE_HOST})
    login_at = login_hosts[0] if login_hosts else SILVERBLAZE_HOST
    if await run.step("login", "utility login", login, at=login_at) is not None:
        await _data_steps(http, run, raw, secrets, secrets[-1], tz)

    if raw["failed_step"] is None:
        raw["verdict"] = COMPATIBLE
    elif raw["failed_step"] == "login":
        raw["verdict"] = INCOMPATIBLE
    else:
        raw["verdict"] = PARTIAL
    return raw, secrets


# --- command line ------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="silverblaze-probe",
        description=(
            "Check whether a utility looks compatible with the Silverblaze integration. "
            "Credentials are prompted interactively and never stored."
        ),
        allow_abbrev=False,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--utility", metavar="SLUG", help="probe a registered utility")
    mode.add_argument("--portal-url", metavar="URL", help="discover an unknown utility portal")
    parser.add_argument("--version", action="version", version=f"%(prog)s {PROBE_VERSION}")
    return parser


async def _run(args: argparse.Namespace, username: str, password: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        if args.utility:
            module = get_utility(args.utility)
            policy = build_allowlist(module.login_endpoints())
            http = SilverblazeHttp(session, policy, **_HTTP_KWARGS)
            raw, secrets = await run_known(http, module, username, password)
        else:
            policy = DiscoveryPolicy(args.portal_url)
            http = SilverblazeHttp(session, policy, **_HTTP_KWARGS)
            raw, secrets = await run_discovery(
                http, policy, args.portal_url, username, password, _discovery_data_steps
            )
    raw["generated_at"] = date.today()
    raw["_secrets"] = secrets
    return raw


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.utility and args.utility not in get_utilities():
        parser.error(
            f"unknown utility {args.utility!r}; known: {', '.join(sorted(get_utilities()))}"
        )
    if args.portal_url:
        try:
            DiscoveryPolicy(args.portal_url)
        except ValueError:
            parser.error("--portal-url must be a plain https:// URL (no credentials or port)")

    username = input("Utility username: ")
    password = getpass("Utility password (hidden): ")
    try:
        raw = asyncio.run(_run(args, username, password))
    finally:
        del password

    secrets = raw.pop("_secrets", [username])
    print(f"Verdict: {raw.get('verdict', INCOMPATIBLE)}")
    if raw.get("failed_step"):
        print(f"Failed step: {raw['failed_step']}")
    path = write_report(build_report(raw, secrets))
    print(f"\nReport written: ./{path.name}")
    print('Review it before attaching it to a "Utility support request" issue.')
    print(f"It contains no {_EXCLUDED}.")
    return 0 if raw.get("verdict") == COMPATIBLE else 1


if __name__ == "__main__":
    raise SystemExit(main())
