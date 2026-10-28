# Setup

The quick way:

```sh
uv tool install git+https://github.com/kurok/busybar-calendar
busybar-calendar init      # answers in, config out: the bar, the calendar, the passwords
busybar-calendar check     # proves it works, or says what to fix
busybar-calendar install   # runs it in the background from now on
busybar-calendar status    # and later: is it running, and what is on the bar next
```

The rest of this page is what those commands do for you, for when you
want to do it yourself, or need more than one calendar source. Every option
of the config is in the [configuration reference](configuration.md).

1. [Connect to the bar](#connect-to-the-bar)
2. [Add your calendars](#add-your-calendars)
3. [Keep secrets out of files](#keep-secrets-out-of-files)
4. [Check it](#check-it)
5. [Run it as a service](#run-it-as-a-service)
6. [Troubleshooting](troubleshooting.md)

## Connect to the bar

There are two ways to reach the bar. They draw exactly the same things.

### Through BUSY Cloud (simplest)

This is the route the bar's Google Calendar integration takes, and it works
from anywhere, with nothing to enable on the bar.

1. In your BUSY account, create an API token and choose **BUSY Bar** under
   **Access scope**.
2. Store it (see [secrets](#keep-secrets-out-of-files)) and give it to
   `[bar]` without an `address`:

```toml
[bar]
token_command = ["security", "find-generic-password", "-s", "busybar-token", "-w"]
```

It is sent as `Authorization: Bearer ...` to `https://api.busy.app/busybar`.

### On the local network

Faster, and does not depend on the cloud, but the bar's HTTP API over Wi-Fi
is **disabled by default**.

1. Connect the bar to your computer by USB and open `http://10.0.4.20`.
2. On the **Network** tab, in **HTTP API**, choose **Set password and
   enable**. Keep the password to **32 characters or fewer**. The firmware
   refuses longer ones.
3. Find the bar's address: **Settings -> Wi-Fi -> your network -> View IP
   Address**.

```toml
[bar]
address = "192.168.1.50"
token_command = ["security", "find-generic-password", "-s", "busybar-lan", "-w"]
```

The password is sent as `X-API-Token`. To check the bar's mode without a
password: `curl http://<address>/api/access` returns `{"mode":"disabled"}`,
`"enabled"` or `"key"`. While it says `disabled`, every other request over
Wi-Fi gets `403 Forbidden`, whatever token you send. Over USB
(`address = "10.0.4.20"`) no password is needed at all.

## Add your calendars

Each `[[sources]]` entry is one CalDAV account or one ICS feed. A CalDAV
account uses all of its calendars unless you [filter them](#choosing-calendars).

`busybar-calendar add` adds one more by asking the same questions as `init`,
and appends it to your config without touching the rest, then restarts a
background service so it reads the new calendar at once.

A running service also follows edits you make by hand: they apply within a
minute. An edit that does not load leaves it on the config it has, and its
log says what is wrong; a change under `[bar]` waits for
`busybar-calendar restart`.

### CalDAV

| Provider | `url` | Password |
| --- | --- | --- |
| iCloud | `https://caldav.icloud.com` | an app-specific password (account.apple.com -> Sign-In and Security -> App-Specific Passwords) |
| Fastmail | `https://caldav.fastmail.com` | an app password (Settings -> Privacy & Security) |
| Nextcloud | `https://cloud.example.com/remote.php/dav` | your password, or an app password |
| PrivateEmail (Namecheap) | `https://dav.privateemail.com` | your mailbox password |
| Radicale, Baïkal, Synology, others | the server's root URL, or a calendar's URL | as the server wants it |

`username` is usually your full email address. A root URL is enough when
the server supports discovery (`/.well-known/caldav`); otherwise give the
URL of one calendar, and it is used as it is. Servers that only speak
Digest auth need `auth = "digest"`.

```toml
[[sources]]
name = "iCloud"
type = "caldav"
url = "https://caldav.icloud.com"
username = "me@icloud.com"
password_command = ["security", "find-generic-password", "-s", "busybar-icloud", "-w"]
include = ["Home", "Work"]

[sources.calendars.Work.status]
show = "meeting"
```

### Choosing calendars

`busybar-calendar calendars` lists each calendar on the account with its
**id** (the last part of its URL) and **name**, and marks the ones in use:

```
PrivateEmail (caldav): 22 calendars, 1 used
  * 4711   Doe, Jane
    5001   Ops Maintenances
    5002   Infra Maintenances
    6001   Office - Room 1 (6 people)
    ...
```

Two lists filter them. Each entry is a calendar's **name**, its **id**, or
its **URL**, and may use the wildcards `*` and `?`. Matching is exact and
case-sensitive.

| | |
| --- | --- |
| `include` | Use only these calendars. Without it, all of them. |
| `exclude` | Leave these out, after `include`. |

```toml
include = ["Doe, Jane"]                 # by name
include = ["4711"]                     # by id: survives a rename
include = ["Doe, Jane", "* Maintenances"]
exclude = ["Office - Room *", "Room *"]   # everything except the meeting rooms
```

Organisation accounts often expose shared calendars. A PrivateEmail
business mailbox, for example, lists every meeting room and team calendar
next to your own. With no filter, other people's room bookings would reach
your bar, so name your own calendar under `include`.

Calendars come in the order `include` lists them: when two events start at
the same time, the calendar listed last wins. An entry that matches nothing
is reported, by `calendars` and in the service's log.

Per-calendar settings can be keyed by name or by id, and the id wins when
both are given:

```toml
[sources.calendars."4711".reminder]
minutes_before = 10
```

Your answer to each invitation is read from your attendee entry, found by
your address: declined invitations are skipped, and when two events overlap,
the one you accepted wins over one you marked tentative or have not
answered - whether they start together or one is shifted against the other.
An event you marked tentative ("Maybe") is drawn with a yellow calendar and
a yellow BUSY instead of the red ones. The address defaults to `username` when that is an email address;
set `emails = [...]` if your invitations go to another one.

### ICS / webcal feeds

For calendars that only publish a link. A feed is one calendar, named after
its source. `webcal://` links work as they are.

| Calendar | Where to find the link |
| --- | --- |
| Outlook / Microsoft 365 | Settings -> Calendar -> Shared calendars -> **Publish a calendar** -> ICS link |
| Google Calendar | Settings -> the calendar -> Integrate calendar -> **Secret address in iCal format** |
| Proton Calendar | the calendar's menu -> Share -> **Share with anyone** (link) |

```toml
[[sources]]
name = "Outlook"
type = "ics"
url = "https://outlook.office365.com/owa/calendar/.../calendar.ics"
```

A published feed leaves out who is invited, so your own answer to an invitation
is not in it; an event the feed marks tentative (Outlook does, as its busy
status) still counts as tentative. Outlook's Windows timezone names are
understood. A feed is only as fresh as the provider makes it. Outlook and Google can
take a while to republish a change, while CalDAV sees it on the next poll
(`poll_minutes`, default 5).

## Keep secrets out of files

Every password and token takes one of three forms. Prefer the last two:

```toml
password = "..."                       # inline: only for throwaway testing
password_env = "ICLOUD_APP_PASSWORD"   # an environment variable
password_command = ["security", "find-generic-password", "-s", "busybar-icloud", "-w"]
```

The same goes for the bar: `token`, `token_env`, `token_command`. A command
is run without a shell and its output, stripped, is the secret.

On macOS, store a secret in the Keychain from your own terminal. It prompts,
so the secret ends up neither on screen nor in your shell history:

```sh
security add-generic-password -U -s busybar-icloud -a me@icloud.com -w
```

On Linux, `secret-tool` (libsecret) or `pass` work the same way:
`password_command = ["secret-tool", "lookup", "service", "busybar-icloud"]`.

`config.toml` is in `.gitignore`. Keep it that way even with no secrets in
it, since it names your accounts.

## Check it

```sh
busybar-calendar check
```

checks the whole setup and says what to fix:

```
Bar
  ok   BUSY Cloud: firmware 1.2.4, API 27.5.0, token accepted
Calendars
  ok   iCloud: signed in; 2 of 5 calendars used: Home, Work
  ok   iCloud: 23 events in the next 7 days; next Tue 13 Oct 09:00 Standup

All good.
```

A refused bar call comes with its cause: the Wi-Fi HTTP API switched off,
a missing password, or a cloud token given as a LAN password. The other
commands look closer:

```sh
busybar-calendar calendars                 # discovery: calendars per CalDAV source
busybar-calendar upcoming --hours 48       # events, settings, your response, what is on now
busybar-calendar demo                      # a sample reminder for 20 seconds
busybar-calendar demo --status --show meeting
busybar-calendar demo --font global --title "Планування спринту"
busybar-calendar replay                    # tomorrow's real events, one a minute
busybar-calendar replay --day today --seconds 30 --quiet
busybar-calendar -v run                    # run in the foreground with debug logs
```

## Run it as a service

```sh
busybar-calendar install
```

checks the setup, then makes it run in the background from now on: at
login, and again if it stops. On macOS that is a launch agent
(`~/Library/LaunchAgents/app.busybar.calendar.plist`, logging to
`~/Library/Logs/busybar-calendar.log`); on Linux a systemd user unit
(`~/.config/systemd/user/busybar-calendar.service`, logs in
`journalctl --user -u busybar-calendar`).

- It runs from the environment you installed from (a `uv tool` or a
  virtualenv) and reads the config `install` found, wherever you start it.
- Restarts are five minutes apart, so a calendar that refuses your password
  is not asked again every few seconds.
- A launch agent runs in your login session, so it can read the login
  Keychain for `token_command` / `password_command`.
- `--dry-run` shows the file and the commands without changing anything;
  `--force` installs even when `check` finds a problem.
- `--auto-update` makes it [update itself](#updating-by-itself) every time
  it starts.
- `busybar-calendar uninstall` stops and removes it.

`busybar-calendar status` shows how it is doing:

```
busybar-calendar 0.3.1
Service   running 0.3.1, pid 4321
          log /Users/me/Library/Logs/busybar-calendar.log
            16:40:19 INFO busybar_calendar.runner: reminder: Design review (Work, accepted) ...
            16:45:00 INFO busybar_calendar.runner: status: Design review (Work, accepted) ...
Bar       BUSY Cloud: answering
Calendar  now status: Design review, until Tue 13 Oct 17:30 (in 44m)
          next reminder: Retro, at Tue 13 Oct 17:55 (in 1h 09m)
```

A stopped service shows its last exit code, with the log lines that explain it.

Both `status` and `check` take `--json`, for scripts, dashboards or a menu-bar
widget. `check --json` exits non-zero when something fails, like `check`:

```sh
busybar-calendar status --json | jq -r '.calendar.next | "\(.kind) \(.title) at \(.at)"'
busybar-calendar check --json | jq '.findings[] | select(.ok | not) | .message'
```

### Upgrading

Upgrade the install, then restart the service, which keeps running the old
version until then:

```sh
uv tool upgrade busybar-calendar   # fetches the newest from git, for the command and the service
busybar-calendar restart
```

That works for the one-line install from git. An install from a clone takes
`git pull && uv tool install --force .`, and one from a release file or a
tag (`...@v0.3.1`) takes `uv tool install --force` with the new one. Your
config and passwords stay where they are.

`status` shows which version the service runs, and says when it is not the
one just installed:

```
Service   running 0.3.0, pid 4321
          that is 0.3.0, but this is 0.3.1: `busybar-calendar restart` runs this one
```

When the command you run is from another install than the service (a
checkout run with `uv run`, say), a restart would start the old version
again, so `status` names the install to upgrade instead:

```
          that is 0.3.0, from ~/.local/share/uv/tools/busybar-calendar, not this 0.3.1:
          `uv tool upgrade busybar-calendar`, then `busybar-calendar restart`
```

It gives the `uv tool upgrade` command when that install is a uv tool from
git, and says "upgrade that one" otherwise.

#### Updating by itself

```sh
busybar-calendar install --auto-update
```

installs the service so that every start - at login, after a restart, or
after it stopped - first runs `uv tool upgrade busybar-calendar`, then
starts the newest version. It comes before the config is read, so a config
that uses something new gets the version that understands it.

- It needs the one-line install from git (`uv tool install
  git+https://github.com/kurok/busybar-calendar`): that is the install
  `uv tool upgrade` moves forward, to the newest commit on `main`. `install`
  refuses `--auto-update` for any other install, and when there is no `uv`.
- If the update fails - no network yet at login, say - the service starts
  the version it has, and tries again at its next start. Each start logs
  what happened:

  ```
  INFO busybar_calendar.update: auto-update: 0.5.1 (a0c7935) -> 0.6.1 (0046d3c)
  INFO busybar_calendar: busybar-calendar 0.6.1 started: 1 calendar source(s), bar via BUSY Cloud
  ```

- So `busybar-calendar restart` is also how to update now, and `status`
  says so: `Service   running 0.6.1, pid 4321, updates itself at each start`.
- `install` without the flag turns it off again.

### Docker

```sh
docker build -t busybar-calendar .
docker run -d --restart unless-stopped \
  -e BUSYBAR_TOKEN -e CALDAV_PASSWORD \
  -v "$PWD/config.toml:/config/config.toml:ro" busybar-calendar
```

In a container, use `token_env` and `password_env`, since the Keychain is
not there.

## Troubleshooting

See [troubleshooting.md](troubleshooting.md): what `check` and `status` say
for each common problem, and what to do about it.
