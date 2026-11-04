# Contributing

Thanks for helping. The most useful contribution is support for another utility that
uses a Silverblaze usage portal.

## Development setup

```
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## Data and commit rules

- **No personal data anywhere in the repository**: code, tests, fixtures, docs, issues, pull
  requests and commit messages. That means no account numbers, service point or meter IDs,
  real usernames or e-mail addresses, addresses, real kWh values, cookies or tokens. Use
  synthetic values: `user@example.com`, `SP0000000001`, `SYNTHETIC_...` placeholders, round
  or obviously made-up numbers.
- **Fixtures are redacted or synthetic** (see "Record and redact fixtures" below).
- **Credentials** are never logged, never stored outside the Home Assistant config entry and
  never written into a report or fixture.
- **Read-only**: the integration must only ever reach usage endpoints. Nothing that touches
  billing, payments, autopay or profile editing.
- **Commit messages describe the change itself**, for example "Add hourly usage import with
  DST-aware timestamps". Do not include local paths or
  references to private planning.

## Adding a utility

### 1. Run the probe

The probe checks whether a utility's portal hands off to Silverblaze and describes how its
login works, without storing your credentials. It has two modes:

```
silverblaze-probe --utility <slug>
silverblaze-probe --portal-url <url>
```

`--utility` tests a utility that already has a module. `--portal-url` investigates a new
portal. Run it in your own terminal and type credentials at the prompts; they are not saved.
It writes a `probe-report-<date>-<mode>.json` file in the current directory (git-ignored).
Open the report and review it before sharing it: it should contain no personal data. If
anything looks personal, remove it. Then either open a "Utility support request" issue and
paste or attach the report, or continue with the steps below.

### 2. Write the login module

A module is one small class in `custom_components/silverblaze/api/utilities/`, subclassing
`UtilityModule` (`api/utilities/base.py`). Implement these class methods:

| Member | Returns |
|---|---|
| `name()` | Display name shown in the config flow |
| `slug()` | Lower-case identifier matching `[a-z0-9]+`, used in statistic IDs |
| `client_id()` | The Silverblaze ClientId (informational) |
| `timezone()` | IANA time zone of the utility, for example `America/Denver` |
| `login_endpoints()` | `frozenset` of `(host, path)` pairs the login may contact |
| `async login(http, username, password)` | The Silverblaze `enc` string |

Rules:

- Do all network I/O through the `SilverblazeHttp` object passed to `login`. It enforces the
  endpoint allowlist; never open your own session.
- Send the credential POST with `max_retries=0` so a failed request is never replayed.
- Raise `InvalidAuth` for rejected credentials, `CannotConnect` for network or server
  problems, and `LoginFlowChanged` when the portal no longer looks the way the module
  expects. These come from `custom_components.silverblaze.api`.
- Never log or include credentials in exception messages.
- Subclassing registers the module automatically. A duplicate or invalid slug raises
  `ValueError` at import time.

`custom_components/silverblaze/api/utilities/idahopower.py` is a complete example, and
`tests/test_example_utility.py` shows a minimal synthetic one.

### 3. Register it

Add an import to `custom_components/silverblaze/api/utilities/__init__.py`:

```python
from . import yourutility  # noqa: F401  (import registers the module)
```

The utility then appears in the config-flow dropdown, and its `login_endpoints()` join the
allowlist (`build_allowlist` combines them with the shared Silverblaze endpoints). Do not
change the generic client, HTTP layer or allowlist code to make a module work.

### 4. Record and redact fixtures

Record real responses locally, then replace every one of these with a synthetic placeholder
before committing:

- [ ] tokens and cookies
- [ ] `enc` values
- [ ] `parameters` values
- [ ] account, customer, premise and collection IDs
- [ ] names and addresses
- [ ] query strings
- [ ] kWh values (use round or made-up numbers)

Use placeholders such as `SYNTHETIC_...`, `user@example.com` and `SP0000000001`. Put files
under `tests/fixtures/<slug>/`, then run:

```
.venv/bin/python -m pytest tests/test_fixture_hygiene.py
```

It fails on e-mail addresses, long opaque strings, JWTs and similar leftovers. Also read the
fixtures yourself; the scanner is a safety net, not a guarantee.

### 5. Tests your module must ship

- Login happy path (returns the expected `enc`).
- Invalid credentials raise `InvalidAuth`.
- A changed portal flow raises `LoginFlowChanged`.
- Allowlist entries: the module's `login_endpoints()` are exactly what you expect, and
  requests outside them are refused.

Follow `tests/test_idahopower_login.py` for structure (mocked responses, synthetic fixtures).

### 6. What "supported" means

A utility is marked supported once a contributor has verified it live with Home Assistant:
a successful historical backfill and one normal update cycle, using their own account.
Report this in the pull request (the template has a checkbox). Do not paste usage values or
identifiers; say only that it worked.
