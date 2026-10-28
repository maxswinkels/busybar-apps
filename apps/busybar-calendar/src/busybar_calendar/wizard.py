"""
`busybar-calendar init`: a few questions instead of editing a TOML file.

It asks how to reach the bar and which calendar to read, signs in to list
the calendars, and writes the config. Passwords never pass through this
program: on macOS the Keychain's own prompt (`security ... -w`) reads them,
on Linux libsecret's (`secret-tool store`), and the config only names the
command that reads them back. Without either, the config names an
environment variable to set instead.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sources
from .caldav import CalDAVError, RemoteCalendar
from .config import Config, ConfigError, SourceConfig, load

Ask = Callable[[str], str]
USB_HOST = "10.0.4.20"


# Servers to suggest by the address's domain; anything else is asked for.
@dataclass(frozen=True)
class Provider:
    label: str
    url: str = ""  # empty: asked for
    ask_for: str = ""  # what to ask, when the URL depends on the user's server
    note: str = ""  # said before the password is asked for
    domains: tuple[str, ...] = ()  # email domains that suggest it


PROVIDERS = (
    Provider(
        "iCloud",
        "https://caldav.icloud.com",
        note="iCloud needs an app-specific password: account.apple.com -> Sign-In and "
        "Security -> App-Specific Passwords.",
        domains=("icloud.com", "me.com", "mac.com"),
    ),
    Provider(
        "Fastmail",
        "https://caldav.fastmail.com",
        note="Fastmail needs an app password: Settings -> Privacy & Security.",
        domains=("fastmail.com", "fastmail.fm"),
    ),
    Provider("PrivateEmail", "https://dav.privateemail.com"),
    Provider("Nextcloud", ask_for="Your Nextcloud's address, e.g. https://cloud.example.com"),
    Provider("Another server", ask_for="The CalDAV server's URL"),
)
NAMES = {
    "PrivateEmail": "PrivateEmail (Namecheap)",
    "Nextcloud": "Nextcloud: asks for your server",
    "Another server": "Another server: Radicale, Baikal, Synology, Zoho, ... asks for its URL",
}


def _server(ask: Ask, domain: str) -> tuple[str, Provider]:
    """Pick the provider - guessed from the address - and the server URL it implies."""
    guess = next((n for n, p in enumerate(PROVIDERS, 1) if domain in p.domains), len(PROVIDERS))
    number = _choose(
        ask, "Which provider?", [NAMES.get(p.label, p.label) for p in PROVIDERS], guess
    )
    provider = PROVIDERS[number - 1]
    if provider.url:
        return provider.url, provider
    answer = _text(ask, provider.ask_for).rstrip("/")
    if not answer.startswith(("http://", "https://")):
        answer = "https://" + answer
    if provider.label == "Nextcloud" and not answer.endswith("/remote.php/dav"):
        answer += "/remote.php/dav"
    return answer, provider


@dataclass
class Secret:
    """How the config gets a secret back: a command, or an environment variable."""

    command: list[str] | None = None
    env: str | None = None

    def toml(self, key: str) -> str:
        if self.command:
            return f"{key}_command = {json.dumps(self.command, ensure_ascii=False)}"
        return f"{key}_env = {json.dumps(self.env)}"


class SecretStore:
    """Saves a secret with the system's own prompt, so it never passes through here."""

    def __init__(self, platform: str = sys.platform) -> None:
        self.platform = platform

    def save(self, service: str, account: str, env: str) -> Secret:
        if self.platform == "darwin" and shutil.which("security"):
            print("The Keychain asks for it twice (nothing shows as you type):")
            self._store(
                ["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"]
            )
            return Secret(command=["security", "find-generic-password", "-s", service, "-w"])
        if self.platform.startswith("linux") and shutil.which("secret-tool"):
            label = f"busybar-calendar {service}"
            self._store(["secret-tool", "store", "--label", label, "service", service])
            return Secret(command=["secret-tool", "lookup", "service", service])
        print(f"No keychain here: set {env} in the environment the service runs in.")
        return Secret(env=env)

    @staticmethod
    def _store(command: list[str], attempts: int = 3) -> None:
        """Run the keychain's own prompt; ask again when it fails, say a mistyped retype."""
        for attempt in range(1, attempts + 1):
            if subprocess.run(command, check=False).returncode == 0:
                return
            if attempt < attempts:
                print("That was not stored (did the two entries differ?). Once more:")
        raise ConfigError(f"{command[0]} did not store the password; run the command again")


def _choose(ask: Ask, question: str, options: list[str], default: int = 1) -> int:
    print(question)
    for number, option in enumerate(options, 1):
        print(f"  {number}  {option}")
    while True:
        answer = ask(f"Choose 1-{len(options)} [{default}]: ").strip() or str(default)
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print("  Please type one of the numbers.")


def _text(ask: Ask, question: str, default: str = "") -> str:
    while True:
        prompt = f"{question} [{default}]: " if default else f"{question}: "
        answer = ask(prompt).strip() or default
        if answer:
            return answer


@dataclass
class Answers:
    bar_lines: list[str] = field(default_factory=list)
    source_lines: list[str] = field(default_factory=list)

    def toml(self) -> str:
        return "\n".join(
            [
                "# Written by `busybar-calendar init`. Every setting: config.example.toml.",
                "",
                "[bar]",
                *self.bar_lines,
                "",
                "[[sources]]",
                *self.source_lines,
                "",
            ]
        )


def ask_bar(ask: Ask, store: SecretStore, answers: Answers) -> None:
    how = _choose(
        ask,
        "How should it reach the BUSY Bar?",
        [
            "BUSY Cloud: works from anywhere; needs a token from your BUSY account "
            '("BUSY Bar" access scope)',
            "Wi-Fi: the bar's address; needs its HTTP API enabled, with a password",
            f"USB cable: {USB_HOST}, no password",
        ],
    )
    if how == 3:
        answers.bar_lines.append(f"address = {json.dumps(USB_HOST)}")
        return
    if how == 2:
        address = _text(ask, "The bar's address (Settings -> Wi-Fi -> View IP Address)")
        answers.bar_lines.append(f"address = {json.dumps(address)}")
        print("Its HTTP API password (32 characters at most):")
    else:
        print("The BUSY Cloud token:")
    secret = store.save("busybar-calendar-bar", "busybar-calendar", "BUSYBAR_TOKEN")
    answers.bar_lines.append(secret.toml("token"))


def _unique(name: str, taken: frozenset[str]) -> str:
    number, candidate = 2, name
    while candidate in taken:
        candidate, number = f"{name} {number}", number + 1
    return candidate


def ask_calendar(
    ask: Ask,
    store: SecretStore,
    answers: Answers,
    discover: Callable[[SourceConfig], list[RemoteCalendar]],
    taken: frozenset[str] = frozenset(),
) -> str:
    """Ask for one calendar source, add its lines to `answers`, and return its name."""
    kind = _choose(
        ask,
        "Which calendar?",
        [
            "A CalDAV account: iCloud, Fastmail, Nextcloud, PrivateEmail, Radicale, ...",
            "An ICS / webcal link: Outlook's published calendar, Google's secret address, ...",
        ],
    )
    if kind == 2:
        url = _text(ask, "The link")
        name = _unique("Calendar", taken)
        answers.source_lines += [
            f"name = {json.dumps(name)}",
            'type = "ics"',
            f"url = {json.dumps(url)}",
        ]
        return name

    username = _text(ask, "Your username, usually the email address")
    domain = username.rpartition("@")[2].lower()
    url, provider = _server(ask, domain)
    if provider.note:
        print(provider.note)
    print("The calendar password:")
    # One Keychain entry per account: two accounts on one domain must not share it.
    secret = store.save(f"busybar-calendar-{username}", username, "CALDAV_PASSWORD")
    label = provider.label if provider.url else ""  # a fixed provider names the source
    name = _unique(label or domain or "CalDAV", taken)
    lines = [
        f"name = {json.dumps(name)}",
        'type = "caldav"',
        f"url = {json.dumps(url)}",
        f"username = {json.dumps(username)}",
        secret.toml("password"),
    ]
    fields: dict = {"name": name, "type": "caldav", "url": url, "username": username}
    if secret.command:
        fields["password_command"] = secret.command
    else:
        fields["password_env"] = secret.env
    source = SourceConfig.model_validate(fields)
    try:
        found = discover(source)
    except (CalDAVError, sources.FatalSourceError, OSError) as error:
        print(f"Could not sign in: {error}")
        print("The config is written anyway; fix it, then run: busybar-calendar check")
        answers.source_lines += lines
        return name
    print(f"Signed in: {len(found)} calendars.")
    for number, calendar in enumerate(found, 1):
        print(f"  {number:>2}  {calendar.name}  (id {calendar.id})")
    picked = _pick(ask, len(found))
    if picked and len(picked) < len(found):
        names = [found[i - 1].name for i in picked]
        lines.append(f"include = {json.dumps(names, ensure_ascii=False)}")
    answers.source_lines += lines
    return name


def _pick(ask: Ask, count: int) -> list[int]:
    while True:
        answer = ask(
            "Which to show? Numbers separated by commas, Enter for all "
            "(shared calendars such as meeting rooms are better left out): "
        ).strip()
        if not answer:
            return []
        numbers = [part.strip() for part in answer.split(",") if part.strip()]
        if all(n.isdigit() and 1 <= int(n) <= count for n in numbers):
            return sorted({int(n) for n in numbers})
        print(f"  Use numbers from 1 to {count}.")


def _discover(source: SourceConfig) -> list[RemoteCalendar]:
    async def run() -> list[RemoteCalendar]:
        item = sources.CalDAVSource(source, 0, ZoneInfo("UTC"), True)
        try:
            return await item.calendars()
        finally:
            await item.aclose()

    return asyncio.run(run())


def run(
    path: Path,
    *,
    ask: Ask = input,
    store: SecretStore | None = None,
    discover: Callable[[SourceConfig], list[RemoteCalendar]] = _discover,
    overwrite: bool = False,
) -> int:
    if path.exists() and not overwrite:
        answer = ask(f"{path} exists. Replace it? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Left as it is.")
            return 1
    store = store or SecretStore()
    answers = Answers()
    ask_bar(ask, store, answers)
    print()
    ask_calendar(ask, store, answers, discover)
    text = answers.toml()
    Config.model_validate(tomllib.loads(text))  # what load() will read, checked before saving
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    print(f"\nWrote {path}.")
    print("Next: busybar-calendar check, then busybar-calendar install to keep it running.")
    return 0


def add(
    path: Path,
    *,
    ask: Ask = input,
    store: SecretStore | None = None,
    discover: Callable[[SourceConfig], list[RemoteCalendar]] = _discover,
) -> int:
    """Append one more calendar source to an existing config, keeping the rest as it is."""
    current = load(path)  # also says how to make one, when there is none
    existing = path.read_text()
    answers = Answers()
    taken = frozenset(source.name for source in current.sources)
    name = ask_calendar(ask, store or SecretStore(), answers, discover, taken)
    text = existing.rstrip("\n") + "\n\n[[sources]]\n" + "\n".join(answers.source_lines) + "\n"
    Config.model_validate(tomllib.loads(text))  # the whole file, checked before saving
    path.write_text(text)
    print(f"\nAdded {name!r} to {path}. Next: busybar-calendar check")
    return 0
