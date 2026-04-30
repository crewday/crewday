#!/usr/bin/env bash
# Regenerate the LOC-by-language-over-time chart using git-of-theseus.
#
# Usage:
#   ./scripts/update-loc-chart.sh          # defaults
#   REPO_ROOT=. ./scripts/update-loc-chart.sh
#
# Outputs:
#   .loc-stats/*.json        intermediate analysis data (gitignored)
#   docs/loc-by-language.svg the chart embedded in README.md
#
# Requires: git-of-theseus (pip install git-of-theseus), svgo (npm install -g svgo)

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)}"
STATS_DIR="$REPO_ROOT/.loc-stats"
OUT_SVG="$REPO_ROOT/docs/loc-by-language.svg"
BRANCH="${BRANCH:-main}"
INTERVAL="${INTERVAL:-86400}"

resolve_bin() {
    local name="$1"
    local candidate

    candidate="$(command -v "$name" 2>/dev/null || true)"
    if [ -n "$candidate" ]; then
        echo "$candidate"
        return
    fi

    if [ -x "$HOME/.pyenv/shims/$name" ]; then
        echo "$HOME/.pyenv/shims/$name"
        return
    fi

    for bindir in "$HOME/.pyenv/versions/"*/bin; do
        if [ -x "$bindir/$name" ]; then
            echo "$bindir/$name"
            return
        fi
    done

    for bindir in "$REPO_ROOT/.venv/bin" "$HOME/.local/bin"; do
        if [ -x "$bindir/$name" ]; then
            echo "$bindir/$name"
            return
        fi
    done
}

resolve_python() {
    local console_script="$1"
    local shebang
    local interpreter

    if [ -f "$console_script" ]; then
        IFS= read -r shebang <"$console_script" || true
        case "$shebang" in
            '#!'*/env\ python*)
                interpreter="${shebang##*env }"
                interpreter="${interpreter%% *}"
                command -v "$interpreter" 2>/dev/null || true
                return
                ;;
            '#!'*python*)
                interpreter="${shebang#\#!}"
                interpreter="${interpreter%% *}"
                if [ -x "$interpreter" ]; then
                    echo "$interpreter"
                    return
                fi
                ;;
        esac
    fi

    command -v python3 2>/dev/null || command -v python 2>/dev/null || true
}

ANALYZE="$(resolve_bin git-of-theseus-analyze)"
PYTHON="$(resolve_python "$ANALYZE")"

if [ -z "$ANALYZE" ]; then
    echo "error: git-of-theseus not found. Install with: pip install git-of-theseus" >&2
    exit 1
fi

if [ -z "$PYTHON" ]; then
    echo "error: Python not found. Install Python and git-of-theseus." >&2
    exit 1
fi

mkdir -p "$STATS_DIR" "$(dirname "$OUT_SVG")"

echo "Analyzing $REPO_ROOT (branch=$BRANCH, interval=${INTERVAL}s) ..."
"$ANALYZE" \
    --outdir "$STATS_DIR" \
    --branch "$BRANCH" \
    --interval "$INTERVAL" \
    --ignore '.venv/**' \
    --ignore 'node_modules/**' \
    --ignore '.git/**' \
    --ignore 'uv.lock' \
    --ignore '.loc-stats/**' \
    "$REPO_ROOT"

echo "Generating chart -> $OUT_SVG"
"$PYTHON" "$REPO_ROOT/scripts/render-loc-stack-plot.py" \
    --outfile "$OUT_SVG" \
    --max-n 10 \
    "$STATS_DIR/exts.json"

# SVGO="$(command -v svgo 2>/dev/null || true)"
# if [ -n "$SVGO" ]; then
#     echo "Optimizing with svgo"
#     "$SVGO" "$OUT_SVG"
# else
#     echo "warning: svgo not found, skipping SVG optimization" >&2
# fi

echo "Done."
