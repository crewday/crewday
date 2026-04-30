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
from matplotlib import pyplot


def strip_svg_trailing_whitespace(output_path: Path) -> None:
    if output_path.suffix.lower() != ".svg":
        return

    text = output_path.read_text()
    lines = [line.rstrip() for line in text.splitlines()]
    output_path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""))


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
        top_ranked = sorted(ranked[:max_n], key=lambda j: labels[j])
        y = numpy.array([y[j] for j in top_ranked] + [other_sum])
        labels = [labels[j] for j in top_ranked] + ["other"]

    if normalize:
        y = 100.0 * numpy.array(y) / numpy.sum(y, axis=0)

    ts = [dateutil.parser.parse(t) for t in data["ts"]]
    colors = generate_n_colors(len(labels))

    pyplot.figure(figsize=(16, 12), dpi=120)
    pyplot.style.use("ggplot")
    pyplot.stackplot(ts, numpy.array(y), labels=labels, colors=colors)
    pyplot.legend(loc=2)
    if normalize:
        pyplot.ylabel("Share of lines of code (%)")
        pyplot.ylim([0, 100])
    else:
        pyplot.ylabel("Lines of code")
    pyplot.savefig(output_path)
    pyplot.tight_layout()
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
