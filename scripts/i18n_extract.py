"""Regenerate crew.day gettext catalogs and SPA i18n JSON bundles."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from babel.messages import mofile, pofile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCALE = "en-US"
PLACEHOLDER_LOCALES = ("fr", "es")
PSEUDO_LOCALE = "qps-ploc"
DOMAIN = "messages"

LOCALE_ROOT = Path("app/i18n/locales")
TS_CATALOG = Path("app/web/src/i18n/catalogs/en-US.ts")
BUNDLE_DIRS = (
    Path("app/web/src/i18n/bundles"),
    Path("mocks/web/src/i18n/bundles"),
)

_TS_CATALOG_ENTRY_RE = re.compile(
    r'^\s*(?P<key>"(?:\\.|[^"\\])+?")\s*:\s*(?P<value>"(?:\\.|[^"\\])*")\s*,?\s*$'
)
_T_CALL_RE = re.compile(
    r"""\b(?:t|_|gettext)\(\s*(?P<quote>["'])(?P<key>[^"'{}]+)(?P=quote)"""
)
_NGETTEXT_RE = re.compile(
    r"""\bngettext\(\s*(?P<q1>["'])(?P<one>[^"']+)(?P=q1)\s*,\s*(?P<q2>["'])(?P<many>[^"']+)(?P=q2)"""
)
_JINJA_TRANS_RE = re.compile(r"{%\s*trans\s*%}(?P<key>.*?){%\s*endtrans\s*%}", re.S)

_PSEUDO_MAP = str.maketrans(
    {
        "A": "Å",
        "B": "ß",
        "C": "Ç",
        "D": "Ð",
        "E": "É",
        "F": "Ƒ",
        "G": "Ĝ",
        "H": "Ĥ",
        "I": "Ĩ",
        "J": "Ĵ",
        "K": "Ķ",
        "L": "Ĺ",
        "M": "Ṁ",
        "N": "Ñ",
        "O": "Ö",
        "P": "Ṕ",
        "Q": "Ǫ",
        "R": "Ŕ",
        "S": "Š",
        "T": "Ŧ",
        "U": "Ú",
        "V": "Ṽ",
        "W": "Ŵ",
        "X": "Ẋ",
        "Y": "Ý",
        "Z": "Ž",
        "a": "å",
        "b": "ƀ",
        "c": "ç",
        "d": "ð",
        "e": "é",
        "f": "ƒ",
        "g": "ĝ",
        "h": "ĥ",
        "i": "í",
        "j": "ĵ",
        "k": "ķ",
        "l": "ĺ",
        "m": "ṁ",
        "n": "ñ",
        "o": "ö",
        "p": "ṕ",
        "q": "ǫ",
        "r": "ŕ",
        "s": "š",
        "t": "ŧ",
        "u": "ú",
        "v": "ṽ",
        "w": "ŵ",
        "x": "ẋ",
        "y": "ý",
        "z": "ž",
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    regenerate(args.root)
    return 0


def regenerate(root: Path = ROOT) -> None:
    root = root.resolve()
    english_source = _read_ts_catalog(root / TS_CATALOG)
    existing = _read_existing_catalogs(root)
    keys = sorted(set().union(*existing.values(), english_source, _extract_keys(root)))

    english = {
        key: english_source.get(key, existing.get(DEFAULT_LOCALE, {}).get(key, key))
        for key in keys
    }
    catalogs = {DEFAULT_LOCALE: english}
    for locale in PLACEHOLDER_LOCALES:
        current = existing.get(locale, {})
        catalogs[locale] = {key: current.get(key) or english[key] for key in keys}

    _write_pot(root / LOCALE_ROOT / f"{DOMAIN}.pot", keys)
    for locale, messages in catalogs.items():
        po_path = _po_path(root, locale)
        _write_po(po_path, locale=locale, messages=messages)
        _compile_mo(po_path)

    bundles = {
        **catalogs,
        PSEUDO_LOCALE: {key: _pseudolocalize(value) for key, value in english.items()},
    }
    for bundle_dir in BUNDLE_DIRS:
        _write_bundles(root / bundle_dir, bundles)


def extract_ts_t_calls(
    fileobj: Iterable[str | bytes],
    keywords: object,
    comment_tags: object,
    options: object,
) -> Iterator[tuple[int, str, str, list[str]]]:
    """Babel extractor for TypeScript `t("message.key")` calls."""
    del keywords, comment_tags, options
    for lineno, raw_line in enumerate(fileobj, start=1):
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
        for match in _T_CALL_RE.finditer(line):
            yield lineno, "t", match.group("key"), []


def _read_ts_catalog(path: Path) -> dict[str, str]:
    messages: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _TS_CATALOG_ENTRY_RE.match(line)
        if match is None:
            continue
        key = json.loads(match.group("key"))
        value = json.loads(match.group("value"))
        if isinstance(key, str) and isinstance(value, str):
            messages[key] = value
    return messages


def _read_existing_catalogs(root: Path) -> dict[str, dict[str, str]]:
    catalogs: dict[str, dict[str, str]] = {}
    for po_path in (root / LOCALE_ROOT).glob("*/LC_MESSAGES/*.po"):
        locale = po_path.parent.parent.name
        catalogs[locale] = _read_po(po_path)
    return catalogs


def _read_po(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        catalog = pofile.read_po(fp, locale=path.parent.parent.name.replace("-", "_"))
    messages: dict[str, str] = {}
    for message in catalog:
        if (
            isinstance(message.id, str)
            and message.id
            and isinstance(message.string, str)
        ):
            messages[message.id] = message.string
    return messages


def _po_path(root: Path, locale: str) -> Path:
    return root / LOCALE_ROOT / locale / "LC_MESSAGES" / f"{DOMAIN}.po"


def _extract_keys(root: Path) -> set[str]:
    keys: set[str] = set()
    for path in _iter_source_files(root):
        text = path.read_text(encoding="utf-8")
        keys.update(match.group("key") for match in _T_CALL_RE.finditer(text))
        for match in _NGETTEXT_RE.finditer(text):
            keys.add(match.group("one"))
            keys.add(match.group("many"))
        if path.suffix == ".j2":
            keys.update(
                match.group("key").strip() for match in _JINJA_TRANS_RE.finditer(text)
            )
    return {key for key in keys if key}


def _iter_source_files(root: Path) -> Iterator[Path]:
    scan_roots = (root / "app", root / "app/web/src", root / "mocks/web/src")
    suffixes = {".py", ".j2", ".ts", ".tsx"}
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for path in scan_root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            rel = path.relative_to(root).as_posix()
            if _is_generated_or_test_source(rel):
                continue
            yield path


def _is_generated_or_test_source(rel: str) -> bool:
    return (
        "/i18n/bundles/" in rel
        or "/i18n/catalogs/" in rel
        or rel.endswith(".test.ts")
        or rel.endswith(".test.tsx")
        or rel.endswith(".spec.ts")
        or rel.endswith(".spec.tsx")
    )


def _write_pot(path: Path, keys: Iterable[str]) -> None:
    _write_po(path, locale=None, messages={key: "" for key in keys})


def _write_po(path: Path, *, locale: str | None, messages: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    language = locale or ""
    lines = [
        'msgid ""',
        'msgstr ""',
        _po_string("Project-Id-Version: crewday 0.0.1\n"),
        _po_string(f"Language: {language}\n"),
        _po_string("Content-Type: text/plain; charset=UTF-8\n"),
        "",
    ]
    for key in sorted(messages):
        lines.extend(
            (
                f"msgid {_po_atom(key)}",
                f"msgstr {_po_atom(messages[key])}",
                "",
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _compile_mo(po_path: Path) -> None:
    mo_path = po_path.with_suffix(".mo")
    with po_path.open("r", encoding="utf-8") as fp:
        catalog = pofile.read_po(
            fp, locale=po_path.parent.parent.name.replace("-", "_")
        )
    with mo_path.open("wb") as fp:
        mofile.write_mo(fp, catalog)


def _write_bundles(bundle_dir: Path, bundles: Mapping[str, Mapping[str, str]]) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for locale, messages in bundles.items():
        payload = json.dumps(
            dict(sorted(messages.items())), ensure_ascii=False, indent=2
        )
        (bundle_dir / f"{locale}.json").write_text(f"{payload}\n", encoding="utf-8")


def _po_atom(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _po_string(value: str) -> str:
    return _po_atom(value)


def _pseudolocalize(message: str) -> str:
    parts = re.split(r"(\{[A-Za-z0-9_]+\})", message)
    return "".join(
        part
        if part.startswith("{") and part.endswith("}")
        else _inflate(part.translate(_PSEUDO_MAP))
        for part in parts
    )


def _inflate(value: str) -> str:
    extra = max(1, int(len(value) * 0.3))
    return f"{value} {'~' * extra}" if value else value


if __name__ == "__main__":
    raise SystemExit(main())
