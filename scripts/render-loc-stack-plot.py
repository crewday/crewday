#!/usr/bin/env python3
"""Render git-of-theseus stack data without the NumPy 2 generator bug."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import dateutil.parser
import matplotlib
import numpy

matplotlib.use("Agg")

from git_of_theseus.utils import generate_n_colors
from matplotlib import dates, font_manager, pyplot, ticker

FONT_DIR = Path(__file__).resolve().parents[1] / "app/web/src/assets/fonts"
CHART_FONT_FILES = {
    "Fraunces": FONT_DIR / "fraunces-latin-standard-normal.woff2",
    "Inter Tight": FONT_DIR / "inter-tight-latin-500-normal.woff2",
    "Inter Tight Semibold": FONT_DIR / "inter-tight-latin-600-normal.woff2",
}

LABEL_NAMES = {
    "": "No extension",
    ".cfg": "Config",
    ".css": "CSS",
    ".html": "HTML",
    ".ini": "INI",
    ".js": "JavaScript",
    ".mo": "Gettext MO",
    ".po": "Gettext PO",
    ".pot": "Gettext POT",
    ".py": "Python",
    ".sh": "Shell",
    ".toml": "TOML",
    ".ts": "TypeScript",
    ".tsx": "React TSX",
    "/": "Repo root",
    "app/": "App",
    "cli/": "CLI",
    "deploy/": "Deploy",
    "migrations/": "Migrations",
    "mocks/": "Mocks",
    "scripts/": "Scripts",
    "site/": "Website",
    "tests/": "Tests",
    "Other": "Other",
    "other": "Other",
}


PREFERRED_COLORS = {
    ".py": "#3F6E3B",
    ".tsx": "#4F7CA8",
    ".ts": "#78A7C8",
    ".css": "#B04A27",
    ".html": "#D9A441",
    ".js": "#C28E2D",
    ".sh": "#7A7442",
    ".toml": "#7C5F8E",
    ".cfg": "#8C6D5A",
    ".ini": "#A18B72",
    ".po": "#A85E82",
    ".pot": "#C982A0",
    ".mo": "#8E9AAF",
    "": "#B5AD9F",
    "App + website": "#3F6E3B",
    "Tests": "#B04A27",
    "Mocks": "#D9A441",
    "Supporting code": "#4F7CA8",
    "Other": "#D6CCB7",
    "other": "#D6CCB7",
}

FALLBACK_COLORS = [
    "#3F6E3B",
    "#4F7CA8",
    "#B04A27",
    "#D9A441",
    "#7C5F8E",
    "#A18B72",
    "#2AA198",
    "#C982A0",
    "#7A7442",
    "#8E9AAF",
]


class RegisteredFonts:
    def __init__(self) -> None:
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.display = "DejaVu Sans"
        self.body = "DejaVu Sans"
        self.body_semibold = "DejaVu Sans"

    def cleanup(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None


def register_chart_fonts() -> RegisteredFonts:
    fonts = RegisteredFonts()
    try:
        from fontTools.ttLib import TTFont
    except ModuleNotFoundError:
        return fonts

    fonts._tempdir = tempfile.TemporaryDirectory()
    font_names: dict[str, str] = {}
    for label, source in CHART_FONT_FILES.items():
        if not source.exists():
            continue
        target = Path(fonts._tempdir.name) / f"{source.stem}.ttf"
        converted = TTFont(source)
        converted.flavor = None
        converted.save(target)
        font_manager.fontManager.addfont(target)
        font_names[label] = font_manager.FontProperties(fname=target).get_name()

    fonts.display = font_names.get("Fraunces", fonts.display)
    fonts.body = font_names.get("Inter Tight", fonts.body)
    fonts.body_semibold = font_names.get("Inter Tight Semibold", fonts.body)
    return fonts


def strip_svg_trailing_whitespace(output_path: Path) -> None:
    if output_path.suffix.lower() != ".svg":
        return

    text = output_path.read_text()
    lines = [line.rstrip() for line in text.splitlines()]
    output_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))


def display_label(label: str) -> str:
    if label in LABEL_NAMES:
        return LABEL_NAMES[label]
    if label.startswith("."):
        return label.removeprefix(".").upper()
    return label


def series_colors(labels: list[str]) -> list[str]:
    colors = []
    generated = iter(generate_n_colors(len(labels)))
    for index, label in enumerate(labels):
        if label in PREFERRED_COLORS:
            colors.append(PREFERRED_COLORS[label])
        elif index < len(FALLBACK_COLORS):
            colors.append(FALLBACK_COLORS[index])
        else:
            colors.append(next(generated))
    return colors


def parse_group(raw: str) -> tuple[str, list[str]]:
    name, separator, members = raw.partition("=")
    if not separator or not name or not members:
        raise argparse.ArgumentTypeError(
            'groups must use the form "Display label=source-a,source-b"'
        )
    return name, [member for member in members.split(",") if member]


def apply_groups(
    labels: list[str],
    y: numpy.ndarray,
    groups: list[tuple[str, list[str]]],
) -> tuple[list[str], numpy.ndarray]:
    if not groups:
        return labels, y

    label_indexes = {label: index for index, label in enumerate(labels)}
    grouped_labels = []
    grouped_rows = []
    used_indexes: set[int] = set()

    for group_label, members in groups:
        missing = [member for member in members if member not in label_indexes]
        if missing:
            raise ValueError(
                f"group {group_label!r} references unknown labels: {', '.join(missing)}"
            )
        indexes = [
            label_indexes[member] for member in members if member in label_indexes
        ]
        grouped_labels.append(group_label)
        grouped_rows.append(
            numpy.sum(numpy.array([y[index] for index in indexes]), axis=0)
        )
        used_indexes.update(indexes)

    remainder_indexes = [
        index for index, _label in enumerate(labels) if index not in used_indexes
    ]
    if remainder_indexes:
        grouped_labels.append("Other")
        grouped_rows.append(
            numpy.sum(numpy.array([y[index] for index in remainder_indexes]), axis=0)
        )

    return grouped_labels, numpy.array(grouped_rows)


def render_stack_plot(
    input_path: Path,
    output_path: Path,
    max_n: int,
    normalize: bool,
    title: str,
    legend_title: str,
    groups: list[tuple[str, list[str]]],
) -> None:
    with input_path.open() as input_file:
        data = json.load(input_file)

    y = numpy.array(data["y"])
    labels = data["labels"]
    labels, y = apply_groups(labels, y, groups)

    if y.shape[0] > max_n:
        ranked = sorted(range(len(labels)), key=lambda j: max(y[j]), reverse=True)
        other_rows = numpy.array([y[j] for j in ranked[max_n:]])
        other_sum = numpy.sum(other_rows, axis=0)
        top_ranked = sorted(ranked[:max_n], key=lambda j: y[j][-1], reverse=True)
        y = numpy.array([y[j] for j in top_ranked] + [other_sum])
        labels = [labels[j] for j in top_ranked] + ["Other"]

    if normalize:
        totals = numpy.sum(y, axis=0)
        y = numpy.divide(
            100.0 * numpy.array(y),
            totals,
            out=numpy.zeros_like(y, dtype=float),
            where=totals != 0,
        )

    ts = [dateutil.parser.parse(t) for t in data["ts"]]
    colors = series_colors(labels)
    displayed_labels = [display_label(label) for label in labels]
    fonts = register_chart_fonts()

    try:
        pyplot.style.use("default")
        pyplot.rcParams.update(
            {
                "font.family": fonts.body,
                "font.sans-serif": [fonts.body, "DejaVu Sans"],
                "svg.fonttype": "path",
            }
        )
        figure, axis = pyplot.subplots(figsize=(12, 6.4), dpi=144, layout="constrained")
        axis.stackplot(
            ts,
            numpy.array(y),
            labels=displayed_labels,
            colors=colors,
            linewidth=0.45,
            edgecolor="#FFFCF5",
            alpha=0.96,
        )
        legend_columns = 2 if len(displayed_labels) > 6 else 1
        legend = axis.legend(
            loc="upper left",
            bbox_to_anchor=(0.012, 0.988),
            borderaxespad=0,
            frameon=True,
            fancybox=True,
            framealpha=0.92,
            edgecolor="#D6CCB7",
            facecolor="#FFFCF5",
            prop={"family": fonts.body, "size": 8.6, "weight": 500},
            title=legend_title,
            title_fontproperties={"family": fonts.body_semibold, "size": 9.5},
            labelspacing=0.42,
            handlelength=1.45,
            handletextpad=0.55,
            borderpad=0.72,
            columnspacing=1.0,
            ncols=legend_columns,
        )
        legend.get_title().set_color("#524A3E")
        for text in legend.get_texts():
            text.set_color("#524A3E")
        axis.set_title(
            title,
            loc="left",
            pad=13,
            fontdict={
                "family": fonts.display,
                "fontsize": 19,
                "fontweight": 600,
                "color": "#1F1A14",
            },
        )
        axis.set_xlabel("")
        axis.grid(axis="y", color="#E7E0D1", linewidth=0.9)
        axis.grid(axis="x", visible=False)
        axis.set_facecolor("#FAF7F2")
        figure.patch.set_facecolor("#FAF7F2")
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.spines["bottom"].set_color("#D6CCB7")
        axis.tick_params(axis="both", colors="#524A3E", labelsize=9, length=0, pad=7)
        axis.xaxis.set_major_locator(dates.AutoDateLocator(minticks=4, maxticks=8))
        span_days = (max(ts) - min(ts)).days if ts else 0
        if span_days > 730:
            date_format = "%Y"
        elif span_days > 90:
            date_format = "%b %Y"
        else:
            date_format = "%b %d"
        axis.xaxis.set_major_formatter(dates.DateFormatter(date_format))
        if normalize:
            axis.set_ylabel("Share of lines of code (%)")
            axis.set_ylim([0, 100])
            axis.yaxis.set_major_formatter(ticker.PercentFormatter(xmax=100))
        else:
            axis.set_ylabel("Lines of code")
            axis.yaxis.set_major_formatter(ticker.StrMethodFormatter("{x:,.0f}"))
            axis.margins(y=0.04)
        axis.yaxis.label.set_color("#524A3E")
        axis.yaxis.label.set_fontfamily(fonts.body)
        axis.yaxis.label.set_fontweight(500)
        axis.yaxis.labelpad = 10
        for tick_label in [*axis.get_xticklabels(), *axis.get_yticklabels()]:
            tick_label.set_fontfamily(fonts.body)
            tick_label.set_fontweight(500)
        figure.savefig(
            output_path, bbox_inches="tight", pad_inches=0.12, metadata={"Date": None}
        )
        pyplot.close(figure)
        strip_svg_trailing_whitespace(output_path)
    finally:
        fonts.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot git-of-theseus stack data")
    parser.add_argument(
        "--outfile",
        default="stack_plot.png",
        type=Path,
        help="Output file to store results (default: %(default)s)",
    )
    parser.add_argument(
        "--max-n",
        default=20,
        type=int,
        help='Max number of dataseries; the remainder is rolled into "other"',
    )
    parser.add_argument(
        "--normalize",
        action="store_true",
        help="Normalize the plot to 100%%",
    )
    parser.add_argument(
        "--title",
        default="Lines of code by language",
        help="Chart title (default: %(default)s)",
    )
    parser.add_argument(
        "--legend-title",
        default="Language",
        help="Legend title (default: %(default)s)",
    )
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        type=parse_group,
        help='Aggregate input labels, for example "Product=app/,site/"',
    )
    parser.add_argument("input_path", type=Path)
    args = parser.parse_args()

    render_stack_plot(
        input_path=args.input_path,
        output_path=args.outfile,
        max_n=args.max_n,
        normalize=args.normalize,
        title=args.title,
        legend_title=args.legend_title,
        groups=args.group,
    )


if __name__ == "__main__":
    main()
