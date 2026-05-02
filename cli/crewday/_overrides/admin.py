"""Host-only ``crewday admin`` overrides."""

from __future__ import annotations

import base64
import json
import os
import pathlib
from importlib.resources import files
from typing import Any

import click
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy.orm import Session

from crewday._main import ConfigError
from crewday._overrides import cli_override

__all__ = ["register"]


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _load_app_admin() -> Any:
    try:
        from app.admin import init as admin_init_mod
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    return admin_init_mod


def _load_app_backup() -> Any:
    try:
        from app.admin import backup as admin_backup_mod
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    return admin_backup_mod


def _load_app_rotate_root_key() -> Any:
    try:
        from app.admin import rotate_root_key as rotate_root_key_mod
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    return rotate_root_key_mod


def _load_app_rotate_secrets() -> Any:
    try:
        from app.admin import rotate_secrets as rotate_secrets_mod
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    return rotate_secrets_mod


def _make_uow() -> Any:
    try:
        from app.adapters.db.session import make_uow
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    return make_uow


def _settings() -> Any:
    try:
        from app.config import get_settings
    except Exception as exc:
        raise ConfigError(
            "admin commands must run on the server host with app dependencies installed"
        ) from exc
    get_settings.cache_clear()
    return get_settings()


def _refuse_demo(settings: Any, admin_init_mod: Any) -> None:
    if settings.demo_mode:
        raise ConfigError(admin_init_mod.ADMIN_DEMO_REFUSAL)


def _prepare_data_dir(data_dir: pathlib.Path | None) -> None:
    if data_dir is None:
        return
    os.environ["CREWDAY_DATA_DIR"] = str(data_dir)
    if not os.environ.get("CREWDAY_DATABASE_URL"):
        data_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CREWDAY_DATABASE_URL"] = f"sqlite:///{data_dir / 'crewday.db'}"


def _load_root_key_file(path: pathlib.Path | None) -> None:
    if path is None:
        return
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise click.UsageError("--root-key-file must not be empty")
    os.environ["CREWDAY_ROOT_KEY"] = value


def _generate_root_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def _run_migrations() -> None:
    cfg = AlembicConfig()
    try:
        script_location = files("migrations")
    except ModuleNotFoundError:
        script_location = _REPO_ROOT / "migrations"
    cfg.set_main_option("script_location", str(script_location))
    cfg.set_main_option("prepend_sys_path", str(_REPO_ROOT))
    command.upgrade(cfg, "head")


@click.command(name="init")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=pathlib.Path),
    default=None,
    help=(
        "Data directory for a default SQLite database when CREWDAY_DATABASE_URL "
        "is unset."
    ),
)
@click.option(
    "--root-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read CREWDAY_ROOT_KEY from this file instead of the environment.",
)
@click.pass_obj
def init(
    _ctx: object, *, data_dir: pathlib.Path | None, root_key_file: pathlib.Path | None
) -> None:
    """Initialise deployment settings, migrations, and first-boot keys."""
    admin_init_mod = _load_app_admin()
    make_uow = _make_uow()
    _prepare_data_dir(data_dir)
    _load_root_key_file(root_key_file)
    settings = _settings()
    _refuse_demo(settings, admin_init_mod)
    _run_migrations()

    generated_root_key: str | None = None
    with make_uow() as session:
        assert isinstance(session, Session)
        if not admin_init_mod.is_admin_initialized(session) and not os.environ.get(
            "CREWDAY_ROOT_KEY"
        ):
            generated_root_key = _generate_root_key()
            os.environ["CREWDAY_ROOT_KEY"] = generated_root_key
            settings = _settings()
        result = admin_init_mod.admin_init(
            session,
            settings=settings,
            generated_root_key=generated_root_key,
        )

    payload = {
        "initialized": result.initialized,
        "settings_seeded": result.settings_seeded,
        "llm_provider_model_id": result.llm_provider_model_id,
    }
    if result.generated_root_key is not None:
        click.echo(
            "Generated CREWDAY_ROOT_KEY. Store this immediately; "
            "it will not be shown again.",
            err=True,
        )
        payload["generated_root_key"] = result.generated_root_key
    click.echo(json.dumps(payload, sort_keys=True))


init = cli_override("admin", "init", covers=[])(init)


@click.command(name="invite")
@click.option("--email", required=True, help="Invitee email address.")
@click.option("--workspace", "workspace_slug", required=True, help="Workspace slug.")
@click.option(
    "--role",
    type=click.Choice(["owner", "manager", "worker", "client"], case_sensitive=False),
    default="worker",
    show_default=True,
)
@click.pass_obj
def user_invite(
    _ctx: object,
    *,
    email: str,
    workspace_slug: str,
    role: str,
) -> None:
    """Mint a no-email user invite and print the magic link."""
    admin_init_mod = _load_app_admin()
    make_uow = _make_uow()
    settings = _settings()
    _refuse_demo(settings, admin_init_mod)
    with make_uow() as session:
        assert isinstance(session, Session)
        result = admin_init_mod.invite_user(
            session,
            settings=settings,
            email=email,
            workspace_slug=workspace_slug,
            role=role,
        )
    click.echo(result.url)


user_invite = cli_override("admin user", "invite", covers=[])(user_invite)


@click.command(name="bootstrap")
@click.option("--slug", required=True, help="Workspace slug.")
@click.option("--name", required=True, help="Workspace display name.")
@click.option("--owner-email", required=True, help="First owner email address.")
@click.pass_obj
def workspace_bootstrap(
    _ctx: object,
    *,
    slug: str,
    name: str,
    owner_email: str,
) -> None:
    """Create a workspace + owner seat and print the owner's recovery link."""
    admin_init_mod = _load_app_admin()
    make_uow = _make_uow()
    settings = _settings()
    _refuse_demo(settings, admin_init_mod)
    with make_uow() as session:
        assert isinstance(session, Session)
        result = admin_init_mod.workspace_bootstrap(
            session,
            settings=settings,
            slug=slug,
            name=name,
            owner_email=owner_email,
        )
    click.echo(result.url)


workspace_bootstrap = cli_override("admin workspace", "bootstrap", covers=[])(
    workspace_bootstrap
)


@click.command(name="purge")
@click.option("--person", "person_id", required=True, help="User id to purge.")
@click.option(
    "--workspace-id",
    default=None,
    help="Limit purge to one workspace id. Defaults to every workspace for the user.",
)
@click.option("--dry-run", is_flag=True, help="Report target counts without writing.")
@click.pass_obj
def purge(
    _ctx: object,
    *,
    person_id: str,
    workspace_id: str | None,
    dry_run: bool,
) -> None:
    """Anonymise a person and scrub privacy-sensitive dependent rows."""
    try:
        from app.domain.privacy import purge_person
    except Exception as exc:
        raise ConfigError(
            "admin purge must run on the server host with app dependencies installed"
        ) from exc

    make_uow = _make_uow()
    with make_uow() as session:
        assert isinstance(session, Session)
        result = purge_person(
            session,
            person_id=person_id,
            workspace_id=workspace_id,
            dry_run=dry_run,
        )
    click.echo(
        json.dumps(
            {
                "person_id": result.person_id,
                "workspace_ids": list(result.workspace_ids),
                "anonymized_users": result.anonymized_users,
                "scrubbed_occurrences": result.scrubbed_occurrences,
                "scrubbed_comments": result.scrubbed_comments,
                "scrubbed_expenses": result.scrubbed_expenses,
                "scrubbed_payout_destinations": result.scrubbed_payout_destinations,
                "scrubbed_payslips": result.scrubbed_payslips,
                "deleted_secret_envelopes": result.deleted_secret_envelopes,
                "dry_run": dry_run,
            },
            sort_keys=True,
        )
    )


purge = cli_override("admin", "purge", covers=[])(purge)


@click.command(name="backup")
@click.option(
    "--to",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False, dir_okay=True, path_type=pathlib.Path),
    help="Directory where the .tar.zst backup archive will be written.",
)
@click.option(
    "--keep-daily",
    type=click.IntRange(min=0),
    default=30,
    show_default=True,
)
@click.option(
    "--keep-monthly",
    type=click.IntRange(min=0),
    default=12,
    show_default=True,
)
@click.pass_obj
def backup(
    _ctx: object,
    *,
    out_dir: pathlib.Path,
    keep_daily: int,
    keep_monthly: int,
) -> None:
    """Create a local filesystem deployment backup archive."""
    admin_init_mod = _load_app_admin()
    admin_backup_mod = _load_app_backup()
    settings = _settings()
    _refuse_demo(settings, admin_init_mod)
    result = admin_backup_mod.backup(
        out_dir,
        settings=settings,
        keep_daily=keep_daily,
        keep_monthly=keep_monthly,
    )
    click.echo(
        json.dumps(
            {
                "archive_path": str(result.archive_path),
                "kind": result.manifest.kind,
                "content_sha256": result.manifest.content_sha256,
                "row_counts": result.manifest.row_counts,
                "secret_envelope_count": result.manifest.secret_envelope_count,
                "pruned": [str(path) for path in result.pruned],
            },
            sort_keys=True,
        )
    )


backup = cli_override("admin", "backup", covers=[])(backup)


@click.command(name="restore")
@click.option(
    "--from",
    "bundle",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    help="Backup .tar.zst archive to restore.",
)
@click.option(
    "--legacy-key-file",
    "legacy_key_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    help="Root key file for older secret-envelope fingerprints. Repeatable.",
)
@click.pass_obj
def restore(
    _ctx: object,
    *,
    bundle: pathlib.Path,
    legacy_key_files: tuple[pathlib.Path, ...],
) -> None:
    """Restore a backup archive into the configured deployment paths."""
    admin_init_mod = _load_app_admin()
    admin_backup_mod = _load_app_backup()
    settings = _settings()
    _refuse_demo(settings, admin_init_mod)
    result = admin_backup_mod.restore(
        bundle,
        settings=settings,
        legacy_key_files=legacy_key_files,
    )
    _run_migrations()
    click.echo(
        json.dumps(
            {
                "kind": result.manifest.kind,
                "restored_database": (
                    str(result.restored_database)
                    if result.restored_database is not None
                    else None
                ),
                "restored_files": str(result.restored_files),
                "content_sha256": result.manifest.content_sha256,
            },
            sort_keys=True,
        )
    )


restore = cli_override("admin", "restore", covers=[])(restore)


@click.command(name="rotate-root-key")
@click.option(
    "--new-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read the new root key from an operator-owned 0600 file.",
)
@click.option(
    "--new-key-stdin",
    is_flag=True,
    help="Read the new root key from stdin. Refuses an interactive TTY.",
)
@click.option(
    "--reencrypt",
    is_flag=True,
    help="Re-encrypt legacy secret-envelope rows under the active root key.",
)
@click.option(
    "--finalize",
    is_flag=True,
    help="Purge retired slots after the 72-hour retention window.",
)
@click.option(
    "--finalize-now",
    is_flag=True,
    help="Purge retired slots immediately once no rows still use them.",
)
@click.option(
    "--new",
    "legacy_new_value",
    default=None,
    help="Refused: use --new-key-file or --new-key-stdin.",
)
def rotate_root_key(
    *,
    new_key_file: pathlib.Path | None,
    new_key_stdin: bool,
    reencrypt: bool,
    finalize: bool,
    finalize_now: bool,
    legacy_new_value: str | None,
) -> None:
    """Rotate the deployment root key without an HTTP surface."""
    if legacy_new_value is not None:
        raise click.UsageError(
            "--new <value> is refused because it leaks root keys through shell "
            "history and process listings; use --new-key-file or --new-key-stdin"
        )

    selected = [
        new_key_file is not None,
        new_key_stdin,
        reencrypt,
        finalize,
        finalize_now,
    ]
    if sum(selected) != 1:
        raise click.UsageError(
            "choose exactly one of --new-key-file, --new-key-stdin, --reencrypt, "
            "--finalize, or --finalize-now"
        )
    rotate_mod = _load_app_rotate_root_key()
    make_uow = _make_uow()
    settings = _settings()
    with make_uow() as session:
        assert isinstance(session, Session)
        if new_key_file is not None:
            key = rotate_mod.load_new_key_file(new_key_file)
            try:
                result = rotate_mod.start_rotation(
                    session,
                    settings=settings,
                    new_key=key,
                    new_key_ref=f"file:{new_key_file}",
                )
            finally:
                rotate_mod.zero_key_material(key)
        elif new_key_stdin:
            if os.isatty(0):
                raise click.UsageError("--new-key-stdin refuses an interactive TTY")
            key = rotate_mod.normalise_new_key_material(
                click.get_binary_stream("stdin").read()
            )
            try:
                result = rotate_mod.start_rotation(
                    session,
                    settings=settings,
                    new_key=key,
                    new_key_ref=rotate_mod.ENV_ROOT_KEY_REF,
                )
            finally:
                rotate_mod.zero_key_material(key)
        elif reencrypt:
            result = rotate_mod.reencrypt_legacy_rows(session, settings=settings)
        else:
            result = rotate_mod.finalize_rotation(
                session,
                settings=settings,
                finalize_now=finalize_now,
            )
    click.echo(json.dumps(rotate_mod.result_payload(result), sort_keys=True))


rotate_root_key = cli_override("admin", "rotate-root-key", covers=[])(rotate_root_key)


def _refuse_argv_secret(value: str | None) -> None:
    if value is not None:
        raise click.UsageError(
            "--new <value> is refused because it leaks secrets through shell "
            "history and process listings; use the file or stdin form"
        )


def _safe_input_flags(
    *,
    file_path: pathlib.Path | None,
    stdin: bool,
    unsafe_value: str | None,
    file_label: str,
    stdin_label: str,
) -> None:
    _refuse_argv_secret(unsafe_value)
    if sum((file_path is not None, stdin)) != 1:
        raise click.UsageError(f"choose exactly one of {file_label} or {stdin_label}")


def _stdin_not_tty(option: str) -> None:
    if os.isatty(0):
        raise click.UsageError(f"{option} refuses an interactive TTY")


@click.command(name="rotate-smtp")
@click.option(
    "--new-cred-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read new SMTP credentials from an operator-owned 0600 JSON file.",
)
@click.option(
    "--new-cred-stdin",
    is_flag=True,
    help="Read new SMTP credentials JSON from stdin. Refuses an interactive TTY.",
)
@click.option("--new", "unsafe_new_value", default=None, help="Refused.")
def rotate_smtp(
    *,
    new_cred_file: pathlib.Path | None,
    new_cred_stdin: bool,
    unsafe_new_value: str | None,
) -> None:
    """Rotate deployment SMTP credentials."""
    _safe_input_flags(
        file_path=new_cred_file,
        stdin=new_cred_stdin,
        unsafe_value=unsafe_new_value,
        file_label="--new-cred-file",
        stdin_label="--new-cred-stdin",
    )
    rotate_mod = _load_app_rotate_secrets()
    raw: bytearray
    if new_cred_file is not None:
        raw = rotate_mod.load_secret_file(new_cred_file)
    else:
        _stdin_not_tty("--new-cred-stdin")
        raw = rotate_mod.secret_bytes_from_input(
            click.get_binary_stream("stdin").read()
        )
    credentials = rotate_mod.parse_smtp_credentials(bytes(raw))
    settings = _settings()
    make_uow = _make_uow()
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            result = rotate_mod.rotate_smtp_credentials(
                session,
                settings=settings,
                credentials=credentials,
            )
    finally:
        rotate_mod.zero_key_material(raw)
        rotate_mod.zero_key_material(credentials.password)
    click.echo(json.dumps(rotate_mod.rotation_result_payload(result), sort_keys=True))


@click.command(name="rotate-openrouter")
@click.option(
    "--new-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read the new OpenRouter key from an operator-owned 0600 file.",
)
@click.option(
    "--new-key-stdin",
    is_flag=True,
    help="Read the new OpenRouter key from stdin. Refuses an interactive TTY.",
)
@click.option("--new", "unsafe_new_value", default=None, help="Refused.")
def rotate_openrouter(
    *,
    new_key_file: pathlib.Path | None,
    new_key_stdin: bool,
    unsafe_new_value: str | None,
) -> None:
    """Rotate the deployment OpenRouter key."""
    _safe_input_flags(
        file_path=new_key_file,
        stdin=new_key_stdin,
        unsafe_value=unsafe_new_value,
        file_label="--new-key-file",
        stdin_label="--new-key-stdin",
    )
    rotate_mod = _load_app_rotate_secrets()
    if new_key_file is not None:
        key = rotate_mod.load_secret_file(new_key_file)
    else:
        _stdin_not_tty("--new-key-stdin")
        key = rotate_mod.secret_bytes_from_input(
            click.get_binary_stream("stdin").read()
        )
    settings = _settings()
    make_uow = _make_uow()
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            result = rotate_mod.rotate_openrouter_key(
                session,
                settings=settings,
                api_key=key,
            )
    finally:
        rotate_mod.zero_key_material(key)
    click.echo(json.dumps(rotate_mod.rotation_result_payload(result), sort_keys=True))


@click.command(name="rotate-hmac")
@click.option(
    "--new-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read the new 32-byte HMAC key from an operator-owned 0600 file.",
)
@click.option(
    "--new-key-stdin",
    is_flag=True,
    help="Read the new 32-byte HMAC key from stdin. Refuses an interactive TTY.",
)
@click.option("--new", "unsafe_new_value", default=None, help="Refused.")
def rotate_hmac(
    *,
    new_key_file: pathlib.Path | None,
    new_key_stdin: bool,
    unsafe_new_value: str | None,
) -> None:
    """Rotate deployment HMAC signing keys."""
    _safe_input_flags(
        file_path=new_key_file,
        stdin=new_key_stdin,
        unsafe_value=unsafe_new_value,
        file_label="--new-key-file",
        stdin_label="--new-key-stdin",
    )
    rotate_mod = _load_app_rotate_secrets()
    if new_key_file is not None:
        key = rotate_mod.load_secret_file(new_key_file)
    else:
        _stdin_not_tty("--new-key-stdin")
        key = rotate_mod.secret_bytes_from_input(
            click.get_binary_stream("stdin").read()
        )
    settings = _settings()
    make_uow = _make_uow()
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            result = rotate_mod.rotate_hmac_signing_key(
                session,
                settings=settings,
                new_key=key,
            )
    finally:
        rotate_mod.zero_key_material(key)
    click.echo(json.dumps(rotate_mod.rotation_result_payload(result), sort_keys=True))


@click.command(name="rotate-session-secret")
@click.option(
    "--new-key-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=pathlib.Path),
    default=None,
    help="Read the new 32-byte session signing key from a 0600 file.",
)
@click.option(
    "--new-key-stdin",
    is_flag=True,
    help="Read the new 32-byte session signing key from stdin.",
)
@click.option("--new", "unsafe_new_value", default=None, help="Refused.")
def rotate_session_secret(
    *,
    new_key_file: pathlib.Path | None,
    new_key_stdin: bool,
    unsafe_new_value: str | None,
) -> None:
    """Rotate session signing material and clear sessions."""
    _safe_input_flags(
        file_path=new_key_file,
        stdin=new_key_stdin,
        unsafe_value=unsafe_new_value,
        file_label="--new-key-file",
        stdin_label="--new-key-stdin",
    )
    rotate_mod = _load_app_rotate_secrets()
    if new_key_file is not None:
        key = rotate_mod.load_secret_file(new_key_file)
    else:
        _stdin_not_tty("--new-key-stdin")
        key = rotate_mod.secret_bytes_from_input(
            click.get_binary_stream("stdin").read()
        )
    settings = _settings()
    make_uow = _make_uow()
    try:
        with make_uow() as session:
            assert isinstance(session, Session)
            result = rotate_mod.rotate_session_secret(
                session,
                settings=settings,
                new_key=key,
            )
    finally:
        rotate_mod.zero_key_material(key)
    click.echo(json.dumps(rotate_mod.rotation_result_payload(result), sort_keys=True))


rotate_smtp = cli_override("admin", "rotate-smtp", covers=[])(rotate_smtp)
rotate_openrouter = cli_override("admin", "rotate-openrouter", covers=[])(
    rotate_openrouter
)
rotate_hmac = cli_override("admin", "rotate-hmac", covers=[])(rotate_hmac)
rotate_session_secret = cli_override("admin", "rotate-session-secret", covers=[])(
    rotate_session_secret
)


def _load_app_job_state() -> Any:
    """Lazy import so non-host invocations don't pay the app-package cost."""
    try:
        from app.worker import job_state as job_state_mod
    except Exception as exc:
        raise ConfigError(
            "admin worker commands must run on the server host with app "
            "dependencies installed"
        ) from exc
    return job_state_mod


def _system_clock() -> Any:
    try:
        from app.util.clock import SystemClock
    except Exception as exc:
        raise ConfigError(
            "admin worker commands must run on the server host with app "
            "dependencies installed"
        ) from exc
    return SystemClock()


@click.command(name="reset-job")
@click.argument("job_id")
@click.pass_obj
def worker_reset_job(_ctx: object, *, job_id: str) -> None:
    """Clear the cd-8euz killswitch + failure counter for ``job_id``.

    Driven by an operator after a ``worker.job.killed`` audit row
    surfaces in the activity feed. Resets ``worker_heartbeat.dead_at``
    and ``worker_heartbeat.consecutive_failures`` so the next tick
    runs the body again. Writes a ``worker.job.reset`` deployment
    audit row when a real reset happens.

    Returns one JSON line so the verb composes cleanly with the
    sibling ``crewday admin`` commands' shape (``{"job_id": ...,
    "reset": true|false}``). ``"reset": false`` means the
    ``worker_heartbeat`` row does not exist yet — a job that has
    never run cannot be dead.
    """
    job_state_mod = _load_app_job_state()
    clock = _system_clock()
    reset = job_state_mod.reset_job(job_id=job_id, clock=clock)
    click.echo(json.dumps({"job_id": job_id, "reset": reset}, sort_keys=True))


worker_reset_job = cli_override("admin worker", "reset-job", covers=[])(
    worker_reset_job
)


def _ensure_group(root: click.Group, name: str, *, help_text: str) -> click.Group:
    group = root.get_command(click.Context(root), name)
    if group is None:
        group = click.Group(name=name, help=help_text)
        root.add_command(group)
    if not isinstance(group, click.Group):
        raise RuntimeError(f"existing {name!r} command is not a group")
    return group


def register(root: click.Group) -> None:
    group = _ensure_group(root, "admin", help_text="host-only admin commands")
    group.add_command(init)
    group.add_command(backup)
    group.add_command(purge)
    group.add_command(restore)
    group.add_command(rotate_root_key)
    group.add_command(rotate_smtp)
    group.add_command(rotate_openrouter)
    group.add_command(rotate_hmac)
    group.add_command(rotate_session_secret)

    user = _ensure_group(group, "user", help_text="host-only user admin commands")
    user.add_command(user_invite)

    workspace = _ensure_group(
        group,
        "workspace",
        help_text="host-only workspace admin commands",
    )
    workspace.add_command(workspace_bootstrap)

    worker = _ensure_group(
        group,
        "worker",
        help_text="host-only worker maintenance commands",
    )
    worker.add_command(worker_reset_job)
