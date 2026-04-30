from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from textwrap import dedent

from scripts import i18n_extract


def test_regenerate_writes_catalogs_and_spa_bundles(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)

    i18n_extract.regenerate(tmp_path)

    en_bundle = _read_json(tmp_path / "app/web/src/i18n/bundles/en-US.json")
    fr_bundle = _read_json(tmp_path / "mocks/web/src/i18n/bundles/fr.json")
    es_bundle = _read_json(tmp_path / "app/web/src/i18n/bundles/es.json")
    pseudo_bundle = _read_json(tmp_path / "app/web/src/i18n/bundles/qps-ploc.json")

    assert en_bundle["login.title"] == "Sign in with your passkey"
    assert en_bundle["demo.added"] == "Added from source"
    assert fr_bundle["login.title"] == en_bundle["login.title"]
    assert es_bundle["i18n.testGreeting"] == "Hello, {name}!"
    assert pseudo_bundle["i18n.testGreeting"] != en_bundle["i18n.testGreeting"]
    assert "{name}" in pseudo_bundle["i18n.testGreeting"]
    assert (tmp_path / "app/i18n/locales/en-US/LC_MESSAGES/messages.mo").exists()
    assert (tmp_path / "app/i18n/locales/messages.pot").exists()


def test_regenerate_is_deterministic(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    i18n_extract.regenerate(tmp_path)
    first = _snapshot_outputs(tmp_path)

    i18n_extract.regenerate(tmp_path)

    assert _snapshot_outputs(tmp_path) == first


def test_ts_extractor_yields_t_call_keys() -> None:
    source = dedent(
        """
        const label = t("login.title");
        const other = t('i18n.testGreeting', { name });
        """
    )

    found = list(i18n_extract.extract_ts_t_calls(StringIO(source), None, None, None))

    assert found == [
        (2, "t", "login.title", []),
        (3, "t", "i18n.testGreeting", []),
    ]


def _seed_minimal_tree(root: Path) -> None:
    (root / "app/web/src/i18n/catalogs").mkdir(parents=True)
    (root / "mocks/web/src/i18n/catalogs").mkdir(parents=True)
    catalog = dedent(
        """
        export const enUSMessages = {
          "login.title": "Sign in with your passkey",
          "i18n.testGreeting": "Hello, {name}!",
        } as const;
        """
    )
    (root / "app/web/src/i18n/catalogs/en-US.ts").write_text(catalog, encoding="utf-8")
    (root / "mocks/web/src/i18n/catalogs/en-US.ts").write_text(
        catalog, encoding="utf-8"
    )
    (root / "app/web/src/pages").mkdir(parents=True)
    (root / "app/web/src/pages/LoginPage.tsx").write_text(
        'const title = t("login.title");\nconst added = t("demo.added");\n',
        encoding="utf-8",
    )
    po_dir = root / "app/i18n/locales/en-US/LC_MESSAGES"
    po_dir.mkdir(parents=True)
    (po_dir / "messages.po").write_text(
        dedent(
            """
            msgid ""
            msgstr ""
            "Project-Id-Version: crewday 0.0.1\\n"
            "Language: en-US\\n"
            "Content-Type: text/plain; charset=UTF-8\\n"

            msgid "demo.added"
            msgstr "Added from source"
            """
        ),
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_outputs(root: Path) -> dict[str, str]:
    paths = [
        *sorted((root / "app/i18n/locales").rglob("*")),
        *sorted((root / "app/web/src/i18n/bundles").rglob("*")),
        *sorted((root / "mocks/web/src/i18n/bundles").rglob("*")),
    ]
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file() and path.suffix != ".mo"
    }
