#!/usr/bin/env python3
"""Render git-of-theseus stack data without the NumPy 2 generator bug."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import dateutil.parser
import matplotlib
import numpy

matplotlib.use("Agg")

from git_of_theseus.utils import generate_n_colors
from matplotlib import dates, pyplot, ticker

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
    "other": "Other",
}


PREFERRED_COLORS = {
    ".py": "#356D9D",
    ".tsx": "#2AA198",
    ".ts": "#52A7E8",
    ".css": "#C44E52",
    ".html": "#D98C2B",
    ".js": "#E3B341",
    ".sh": "#6B8E23",
    ".toml": "#7F62B3",
    ".cfg": "#8C6D5A",
    ".ini": "#9A879D",
    ".po": "#B36B9E",
    ".pot": "#D19AB7",
    ".mo": "#8E9AAF",
    "": "#A0A6A8",
    "other": "#D0D4D6",
}


def strip_svg_trailing_whitespace(output_path: Path) -> None:
    if output_path.suffix.lower() != ".svg":
        return

    text = output_path.read_text()
    lines = [line.rstrip() for line in text.splitlines()]
    output_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))


def display_label(label: str) -> str:
    return LABEL_NAMES.get(label, label.removeprefix(".").upper() or label)


def series_colors(labels: list[str]) -> list[str]:
    generated = iter(generate_n_colors(len(labels)))
    colors = []
    for label in labels:
        if label in PREFERRED_COLORS:
            colors.append(PREFERRED_COLORS[label])
        else:
            colors.append(next(generated))
    return colors


def render_stack_plot(
    input_path: Path,
    output_path: Path,
    max_n: int,
    normalize: bool,
) -> None:
    with input_path.open() as input_file:
        data = json.load(input_file)

    y = numpy.array(data["y"])
    labels = data["labels"]

    if y.shape[0] > max_n:
        ranked = sorted(range(len(labels)), key=lambda j: max(y[j]), reverse=True)
        other_rows = numpy.array([y[j] for j in ranked[max_n:]])
        other_sum = numpy.sum(other_rows, axis=0)
        top_ranked = sorted(ranked[:max_n], key=lambda j: y[j][-1], reverse=True)
        y = numpy.array([y[j] for j in top_ranked] + [other_sum])
        labels = [labels[j] for j in top_ranked] + ["other"]

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

    pyplot.style.use("default")
    figure, axis = pyplot.subplots(figsize=(13, 7), dpi=144, layout="constrained")
    axis.stackplot(
        ts,
        numpy.array(y),
        labels=[display_label(label) for label in labels],
        colors=colors,
        linewidth=0.25,
        edgecolor="#ffffff",
        alpha=0.95,
    )
    axis.legend(
        loc="center left",
        bbox_to_anchor=(1.01, 0.5),
        frameon=False,
        fontsize=9,
        title="Language",
        title_fontsize=10,
    )
    axis.set_title("Lines of code by language", loc="left", pad=14, fontsize=18)
    axis.set_xlabel("")
    axis.grid(axis="y", color="#D7DEE2", linewidth=0.8)
    axis.grid(axis="x", visible=False)
    axis.set_facecolor("#F8FAF9")
    figure.patch.set_facecolor("#FFFFFF")
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color("#B8C1C7")
    axis.tick_params(axis="both", colors="#52616A", labelsize=9, length=0)
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
    axis.yaxis.label.set_color("#34434B")
    figure.savefig(
        output_path, bbox_inches="tight", pad_inches=0.18, metadata={"Date": None}
    )
    pyplot.close(figure)
    strip_svg_trailing_whitespace(output_path)


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
    parser.add_argument("input_path", type=Path)
    args = parser.parse_args()

    render_stack_plot(
        input_path=args.input_path,
        output_path=args.outfile,
        max_n=args.max_n,
        normalize=args.normalize,
    )


if __name__ == "__main__":
    main()
