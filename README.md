# ha-silverblaze

Home Assistant custom integration that imports hourly utility usage from the
**Silverblaze ACE** customer portal into long-term statistics, so it shows up in the
Energy dashboard.

Developed with AI assistance (Claude); reviewed and tested by the maintainer against a live account.

## Supported utilities

| Utility     | Commodity | Status | Login style |
|-------------|-----------|--------|-------------|
| Idaho Power | Electric  | ✅     | SSO handoff |

✅ verified by a live run   🧪 module exists, needs a tester   ❌ not compatible

Your utility not listed? Many utilities use the same Silverblaze usage portal. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to check a portal and add support, or open a
[utility support request](../../issues/new?template=utility-support-request.yml).

## Installation

Requires Home Assistant 2026.10 or newer.

**a) Manual copy.** Copy the `custom_components/silverblaze` directory of this repository into
`<config>/custom_components/` on your Home Assistant host, then restart Home Assistant.

If your Home Assistant host is reachable over SSH, `scripts/deploy-to-ha.sh` does the copy for
you:

```
scripts/deploy-to-ha.sh [host] [config-dir]
```

The defaults are host `ha` and config directory `/config`. The remote side uses `sudo` unless
the SSH user is root. The script does not restart Home Assistant; restart it afterwards.

**b) HACS custom repository** (once the repository is public). In HACS, add this repository as a
custom repository of type "Integration", install **Silverblaze (Idaho Power)**, and restart Home Assistant.

After restarting, go to **Settings > Devices & services > Add integration**, choose
**Silverblaze (Idaho Power)**, pick your utility, sign in with your utility account, and choose
how much usage history to import (all available history is recommended).

## What you get

- An **hourly kWh statistic** for the Energy dashboard, for example
  `Idaho Power electric consumption` (further service points are named `... 2`, `... 3`).
  The statistic id looks like `silverblaze:idahopower_electric_sp0000000001`. Your full
  available history (or the history from a date you choose) is imported in the background after
  setup; this can take several minutes.
- Two **diagnostic sensors**: **Latest data hour** (the newest hour your utility has published)
  and **Last successful update**. Use them in your own automations to notice stale data.
- **Data lag:** utilities publish usage roughly 2-4 days late, so the newest hours are always a
  few days old.
- **Correction window:** the utility sometimes revises recent hours. The integration re-reads
  the last 30 days and updates them. Corrections to data older than 30 days are not picked up.
- **Update interval:** checks every 12 hours by default; configurable from 6 to 24 hours in the
  integration options.
- **Import from:** start the history at a chosen date, during setup or later under **Change
  settings** in the integration options. To go back to importing everything, choose **Import all
  available history** in the options (shown once a date is set). Changing or removing this date
  never deletes hours that were already imported (remove those under Developer tools > Statistics).
- **Electric only, no cost data.** Billing and rates are never read.

## Security

- Your utility username and password are stored **in plaintext** in Home Assistant's `.storage`
  directory (inside the config entry) and are **included in Home Assistant backups**. Treat
  backups accordingly.
- In a browser, the same login can reach **billing**: viewing and paying bills, autopay, payment
  methods and profile editing. The integration is built so that it cannot. Every request, and
  every redirect hop, is checked against an exact (host, path) allowlist before it is sent, and
  anything else is refused without any network traffic.
- The allowlist contains only the login and usage endpoints, all over `https`:
  - `tools.idahopower.com`: `/MyAccount/Usage`, `/MyAccount/signin-oidc`
  - `auth.idahopower.com`: `/connect/authorize`, `/connect/authorize/callback`, `/Account/Login`
  - `acewebsite.silverblaze.com`: `/Page`, `/api/Page/GetPageParameters`,
    `/api/DataDownload/Meta`, `/api/ConsumptionV3/Init`, `/api/ConsumptionV3/Data`
- Your credentials are sent only to the utility's login hosts (`auth.idahopower.com` for Idaho
  Power) and are never logged or written to any report.

## Troubleshooting

- **Re-authentication notification.** The utility rejected the stored password (for example you
  changed it). Open the notification, or **Settings > Devices & services > Silverblaze (Idaho Power)**, and enter
  your current password.
- **"Your utility's login flow changed in a way this version does not understand."** The utility
  changed its sign-in pages and the integration can no longer follow them. Your password is not
  the problem. Check for an integration update; if there is none, open an issue and attach a probe
  report (see below).
- **Odd early-morning hours when clocks fall back.** On the day clocks fall back in November,
  Idaho Power's portal reports 24 hourly values for the 25-hour day: the first 1:00 AM hour has no
  value, the second 1:00 AM is reported as zero, and that night's usage appears combined into the
  2:00 AM hour. The integration stores what the portal reports, so the Energy dashboard shows an
  empty 1:00 AM position, a zero and a tall 2:00 AM bar. The day total matches the portal's;
  only the hour-by-hour split of those early-morning hours is lost and cannot be recovered from
  the portal.
- **Debug logging.** Add this to `configuration.yaml` and restart, or enable debug logging for
  the integration from its page in Devices & services:

  ```yaml
  logger:
    logs:
      custom_components.silverblaze: debug
  ```

  Then look in **Settings > System > Logs** for lines such as `Backfilling N days`,
  `Imported N of M days`, `Finished importing N days`, `Updated N days`, `Fetched <date>: ...`,
  `Session expired; logging in again`, and warnings like `Sync ended early` or
  `Dropped N duplicate hourly slot(s)`.
- **Running the probe.** The probe checks a portal without storing your credentials and writes a
  redacted `probe-report-<date>-<mode>.json` in the current directory. Install the package (Python 3.14+; `pip install .`
  in a checkout) and run:

  ```
  silverblaze-probe --utility idahopower
  silverblaze-probe --portal-url https://usage.example.com/
  ```

  Review the report before sharing it.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To request a new utility, use the
[utility support request](../../issues/new?template=utility-support-request.yml) issue form.

## License

Copyright 2026 xthree. Licensed under the Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
