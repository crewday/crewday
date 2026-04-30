"""Profile configuration for the crew.day CLI."""

from __future__ import annotations

import os
import pathlib
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

import click

from crewday._globals import DEFAULT_OUTPUT, OUTPUT_CHOICES, OutputMode

__all__ = [
    "CONFIG_FILENAME",
    "Config",
    "ConfigFileError",
    "Profile",
    "active",
    "config_path",
    "display_token",
    "load",
    "redact_token",
    "save",
]


CONFIG_FILENAME: Final[str] = "profiles.toml"
_ENV_PREFIX: Final[str] = "env:"
_FIELD_ORDER: Final[tuple[str, ...]] = (
    "base_url",
    "token",
    "default_workspace",
    "output",
    "ca_bundle",
)


class ConfigFileError(click.ClickException):
    """Config/profile failures use the §13 config-error exit slot."""

    exit_code = 5


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    base_url: str
    token: str | None = None
    default_workspace: str | None = None
    output: OutputMode | None = None
    ca_bundle: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    default: str | None = None
    profiles: dict[str, Profile] = field(default_factory=dict)


def config_path() -> pathlib.Path:
    """Return the XDG config path for the profiles file."""
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        return pathlib.Path(xdg_home) / "crewday" / CONFIG_FILENAME
    return pathlib.Path.home() / ".config" / "crewday" / CONFIG_FILENAME


def load(path: pathlib.Path | None = None) -> Config:
    """Load profiles from TOML, returning an empty config when absent."""
    resolved_path = path if path is not None else config_path()
    if not resolved_path.is_file():
        return Config()
    try:
        with resolved_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"{resolved_path} is not valid TOML: {exc}") from exc

    default = _optional_str(raw.get("default"))
    raw_profiles = raw.get("profile", {})
    if not isinstance(raw_profiles, dict):
        raise ConfigFileError("'profile' must be a TOML table")

    profiles: dict[str, Profile] = {}
    for name, value in raw_profiles.items():
        if not isinstance(name, str) or not name:
            raise ConfigFileError("profile names must be non-empty strings")
        if not isinstance(value, dict):
            raise ConfigFileError(f"profile.{name} must be a TOML table")
        profiles[name] = _profile_from_table(name, value)

    if default is not None and default not in profiles:
        raise ConfigFileError(f"default profile {default!r} does not exist")

    return Config(default=default, profiles=profiles)


def save(cfg: Config, path: pathlib.Path | None = None) -> None:
    """Atomically rewrite profiles TOML and force 0600 permissions."""
    resolved_path = path if path is not None else config_path()
    contents = _format_toml(cfg)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{resolved_path.name}.",
        suffix=".tmp",
        dir=resolved_path.parent,
        text=True,
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(contents)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, resolved_path)
        os.chmod(resolved_path, 0o600)
    except Exception:
        with suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def active(
    cli_override: str | None,
    path: pathlib.Path | None = None,
    *,
    resolve_token: bool = True,
) -> Profile:
    """Return the active profile using CLI override, env, then file default."""
    cfg = load(path)
    name = cli_override or os.environ.get("CREWDAY_PROFILE") or cfg.default
    if not name:
        raise ConfigFileError(
            "no active profile (pass --profile, set CREWDAY_PROFILE, or run "
            "'crewday config use <name>')."
        )
    try:
        profile = cfg.profiles[name]
    except KeyError:
        raise ConfigFileError(f"profile {name!r} does not exist") from None
    if resolve_token:
        return _resolve_profile_token(profile)
    return profile


def redact_token(token: str | None) -> str | None:
    """Redact a token, exposing only the last four characters."""
    if token is None:
        return None
    suffix = token[-4:] if len(token) >= 4 else token
    return f"redacted:{suffix}"


def display_token(token: str | None) -> str:
    """Return the safe representation used by ``crewday config show``."""
    if token is None:
        return ""
    if token.startswith(_ENV_PREFIX):
        return token
    return redact_token(token) or ""


def _profile_from_table(name: str, table: dict[str, object]) -> Profile:
    base_url = _required_str(table.get("base_url"), f"profile.{name}.base_url")
    output = _optional_output(table.get("output"), f"profile.{name}.output")
    return Profile(
        name=name,
        base_url=base_url,
        token=_optional_str(table.get("token")),
        default_workspace=_optional_str(table.get("default_workspace")),
        output=output,
        ca_bundle=_optional_str(table.get("ca_bundle")),
    )


def _resolve_profile_token(profile: Profile) -> Profile:
    token = profile.token
    if token is None or not token.startswith(_ENV_PREFIX):
        return profile
    env_name = token[len(_ENV_PREFIX) :]
    if not env_name:
        raise ConfigFileError(f"profile {profile.name!r} has empty env token name")
    env_value = os.environ.get(env_name)
    if env_value is None:
        raise ConfigFileError(
            f"profile {profile.name!r} token references unset environment variable "
            f"{env_name!r}"
        )
    return Profile(
        name=profile.name,
        base_url=profile.base_url,
        token=env_value,
        default_workspace=profile.default_workspace,
        output=profile.output,
        ca_bundle=profile.ca_bundle,
    )


def _format_toml(cfg: Config) -> str:
    lines: list[str] = []
    if cfg.default is not None:
        if cfg.default not in cfg.profiles:
            raise ConfigFileError(f"default profile {cfg.default!r} does not exist")
        lines.append(f"default = {_toml_string(cfg.default)}")
        lines.append("")

    for name in sorted(cfg.profiles):
        profile = cfg.profiles[name]
        if profile.name != name:
            raise ConfigFileError(
                f"profile mapping key {name!r} does not match profile.name "
                f"{profile.name!r}"
            )
        lines.append(f"[profile.{name}]")
        values = {
            "base_url": profile.base_url,
            "token": profile.token,
            "default_workspace": profile.default_workspace,
            "output": profile.output,
            "ca_bundle": profile.ca_bundle,
        }
        for key in _FIELD_ORDER:
            value = values[key]
            if value is not None:
                lines.append(f"{key} = {_toml_string(value)}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _required_str(value: object, field_name: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise ConfigFileError(f"{field_name} must be a non-empty string")


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ConfigFileError(f"expected string config value, got {type(value).__name__}")


def _optional_output(value: object, field_name: str) -> OutputMode | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigFileError(f"{field_name} must be a string")
    if value not in OUTPUT_CHOICES:
        raise ConfigFileError(
            f"{field_name} must be one of {', '.join(OUTPUT_CHOICES)}"
        )
    match value:
        case "json":
            return "json"
        case "yaml":
            return "yaml"
        case "table":
            return "table"
        case "ndjson":
            return "ndjson"
        case _:
            return DEFAULT_OUTPUT


def _toml_string(value: str) -> str:
    for ch in value:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ConfigFileError(
                f"refusing to write control character 0x{ord(ch):02X} to config"
            )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
