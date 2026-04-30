"""Unit tests for :mod:`crewday._config` and ``crewday config`` commands."""

from __future__ import annotations

import os
import pathlib
import stat
import tomllib

from click.testing import CliRunner
from crewday import _config
from crewday._globals import CrewdayContext
from crewday._main import _register_config_commands_once, config_group, root


def _use_tmp_config(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> pathlib.Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "crewday" / "profiles.toml"


def test_config_path_honours_xdg_config_home(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    config_path = _use_tmp_config(tmp_path, monkeypatch)
    assert _config.config_path() == config_path


def test_toml_round_trip_uses_deterministic_field_order(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    config_path = _use_tmp_config(tmp_path, monkeypatch)
    cfg = _config.Config(
        default="prod",
        profiles={
            "prod": _config.Profile(
                name="prod",
                base_url="https://ops.example.com/api/v1",
                token="env:CREWDAY_TOKEN_PROD",
                default_workspace="ops",
                output="table",
                ca_bundle="/etc/crewday/ca.pem",
            ),
            "dev": _config.Profile(
                name="dev",
                base_url="http://127.0.0.1:8100",
                token="dev-secret",
            ),
        },
    )

    _config.save(cfg)

    text = config_path.read_text(encoding="utf-8")
    assert text.splitlines() == [
        'default = "prod"',
        "",
        "[profile.dev]",
        'base_url = "http://127.0.0.1:8100"',
        'token = "dev-secret"',
        "",
        "[profile.prod]",
        'base_url = "https://ops.example.com/api/v1"',
        'token = "env:CREWDAY_TOKEN_PROD"',
        'default_workspace = "ops"',
        'output = "table"',
        'ca_bundle = "/etc/crewday/ca.pem"',
    ]
    assert _config.load() == cfg


def test_save_forces_0600_permissions(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    config_path = _use_tmp_config(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("stale", encoding="utf-8")
    os.chmod(config_path, 0o644)

    _config.save(
        _config.Config(
            default="dev",
            profiles={
                "dev": _config.Profile(
                    name="dev",
                    base_url="http://127.0.0.1:8100",
                )
            },
        )
    )

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600


def test_active_precedence_and_env_token_resolution(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    _use_tmp_config(tmp_path, monkeypatch)
    _config.save(
        _config.Config(
            default="file",
            profiles={
                "file": _config.Profile(
                    name="file",
                    base_url="https://file.example",
                    token="file-token",
                ),
                "env": _config.Profile(
                    name="env",
                    base_url="https://env.example",
                    token="env:CREWDAY_TOKEN_ENV",
                ),
                "cli": _config.Profile(
                    name="cli",
                    base_url="https://cli.example",
                    token="cli-token",
                ),
            },
        )
    )
    monkeypatch.setenv("CREWDAY_PROFILE", "env")
    monkeypatch.setenv("CREWDAY_TOKEN_ENV", "resolved-token")

    assert _config.active(None).name == "env"
    assert _config.active(None).token == "resolved-token"
    assert _config.active("cli").name == "cli"

    monkeypatch.delenv("CREWDAY_PROFILE")
    assert _config.active(None).name == "file"


def test_atomic_write_leaves_no_temp_file(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    config_path = _use_tmp_config(tmp_path, monkeypatch)

    _config.save(
        _config.Config(
            default="dev",
            profiles={
                "dev": _config.Profile(
                    name="dev",
                    base_url="http://127.0.0.1:8100",
                )
            },
        )
    )

    assert config_path.is_file()
    assert list(config_path.parent.glob("*.tmp")) == []
    assert tomllib.loads(config_path.read_text(encoding="utf-8"))["default"] == "dev"


def test_config_commands_show_list_use_and_rm(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    config_path = _use_tmp_config(tmp_path, monkeypatch)
    _config.save(
        _config.Config(
            default="dev",
            profiles={
                "dev": _config.Profile(
                    name="dev",
                    base_url="http://127.0.0.1:8100",
                    token="dev-secret-1234",
                    default_workspace="smoke",
                    output="yaml",
                ),
                "prod": _config.Profile(
                    name="prod",
                    base_url="https://ops.example.com",
                    token="prod-secret-5678",
                ),
            },
        )
    )
    import click

    @click.group(name="crewday")
    @click.pass_context
    def test_root(ctx: click.Context) -> None:
        ctx.obj = CrewdayContext(profile=None, workspace=None, output="json")

    test_root.add_command(config_group)
    runner = CliRunner()

    shown = runner.invoke(test_root, ["config", "show"])
    assert shown.exit_code == 0, shown.output
    assert "name: dev" in shown.output
    assert "redacted:1234" in shown.output
    assert "dev-secret" not in shown.output

    listed = runner.invoke(test_root, ["config", "list"])
    assert listed.exit_code == 0, listed.output
    assert "* dev" in listed.output
    assert "  prod" in listed.output

    used = runner.invoke(test_root, ["config", "use", "prod"])
    assert used.exit_code == 0, used.output
    assert _config.load(config_path).default == "prod"

    removed = runner.invoke(test_root, ["config", "rm", "dev"])
    assert removed.exit_code == 0, removed.output
    cfg = _config.load(config_path)
    assert cfg.default == "prod"
    assert set(cfg.profiles) == {"prod"}


def test_config_show_does_not_resolve_env_token(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    _use_tmp_config(tmp_path, monkeypatch)
    monkeypatch.setenv("CREWDAY_TOKEN_PROD", "resolved-secret-5678")
    _config.save(
        _config.Config(
            default="prod",
            profiles={
                "prod": _config.Profile(
                    name="prod",
                    base_url="https://ops.example.com",
                    token="env:CREWDAY_TOKEN_PROD",
                )
            },
        )
    )
    import click

    @click.group(name="crewday")
    @click.pass_context
    def test_root(ctx: click.Context) -> None:
        ctx.obj = CrewdayContext(profile=None, workspace=None, output="json")

    test_root.add_command(config_group)
    result = CliRunner().invoke(test_root, ["config", "show"])

    assert result.exit_code == 0, result.output
    assert "token: env:CREWDAY_TOKEN_PROD" in result.output
    assert "resolved-secret" not in result.output
    assert "5678" not in result.output


def test_config_list_ignores_missing_env_profile(
    tmp_path: pathlib.Path,
    monkeypatch,
) -> None:
    _use_tmp_config(tmp_path, monkeypatch)
    _config.save(
        _config.Config(
            default="dev",
            profiles={
                "dev": _config.Profile(
                    name="dev",
                    base_url="http://127.0.0.1:8100",
                )
            },
        )
    )
    monkeypatch.setenv("CREWDAY_PROFILE", "missing")
    _register_config_commands_once()

    result = CliRunner().invoke(root, ["config", "list"])

    assert result.exit_code == 0, result.output
    assert "* dev" in result.output
