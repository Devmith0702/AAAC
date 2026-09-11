"""Figures for the report (CLAUDE.md §4.6).

    make figures

Five figures to ``results/figures/`` at 300 dpi, colour-blind-safe, axis units
labelled, all regenerable from the event logs with one target.

Colour choices are not taste. The three-mode categorical palette below was run
through a CVD validator: worst adjacent pair is ΔE 11.4 under protanopia and
24.2 under normal vision, comfortably above the ΔE 8 target. The amber falls
below 3:1 contrast against white, which obligates secondary encoding — so every
bar carries a direct value label and ``report.py`` prints the same numbers as a
table.

Two rules that shape the layout:

* **Never a dual-axis chart.** Figure 4 shows quantities on entirely different
  scales (a rate, a count, a latency), so it is drawn as stacked small multiples
  sharing one time axis, not two y-scales fighting over one frame.
* **Sequential means one hue, light to dark.** The confusion matrix uses a single
  blue ramp; a rainbow would imply an ordering among classes that the counts do
  not have.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

from aaac.evaluation.access_class import AccessClass  # noqa: E402
from aaac.evaluation.experiment import ExperimentResult  # noqa: E402
from aaac.evaluation.metrics import RunMetrics  # noqa: E402
from aaac.evaluation.stats import mean_ci_t  # noqa: E402

DPI = 300

#: Fixed order, never cycled. Validated for CVD separation (see module docstring).
MODE_COLOUR: dict[str, str] = {
    "none": "#0072B2",
    "baseline": "#E69F00",
    "aaac": "#009E73",
}
MODE_ORDER: tuple[str, ...] = ("none", "baseline", "aaac")
CLASS_ORDER: tuple[AccessClass, ...] = (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW)

INK = "#1a1a1a"
MUTED = "#6b6b6b"
GRID = "#d9d9d9"

#: Single-hue sequential ramp for the confusion matrix.
BLUES = LinearSegmentedColormap.from_list("aaac_blues", ["#f2f7fb", "#0072B2"])


def _style(ax: plt.Axes) -> None:
    """Recessive grid and axes; the data carries the ink."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, alpha=0.7)
    ax.set_axisbelow(True)
    ax.xaxis.grid(False)


def _save(fig: plt.Figure, out_dir: Path, name: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    fig.savefig(path, dpi=DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def _ci(values: Sequence[float]) -> tuple[float, float]:
    """Mean and half-width of the 95% CI; half-width 0 when a single seed."""
    if len(values) < 2:
        return (values[0] if values else 0.0), 0.0
    interval = mean_ci_t(values)
    return interval.mean, max(0.0, interval.high - interval.mean)


# --------------------------------------------------------------------------
# figure 1 — per-class completion rate
# --------------------------------------------------------------------------


def fig_completion_rate(result: ExperimentResult, out_dir: Path) -> Path:
    """Grouped bars: three modes x three classes, with 95% CI error bars.

    §4.6 calls this "the slide that carries the presentation".
    """
    fig, ax = plt.subplots(figsize=(8, 4.8))
    width = 0.26
    positions = range(len(CLASS_ORDER))

    for i, mode in enumerate(m for m in MODE_ORDER if m in result.modes):
        means, errs = [], []
        for cls in CLASS_ORDER:
            per_seed = [
                r.metrics.per_class[int(cls)].completion_rate for r in result.for_mode(mode)
            ]
            mean, half = _ci(per_seed)
            means.append(mean)
            errs.append(half)
        offset = (i - 1) * width
        bars = ax.bar(
            [p + offset for p in positions], means, width * 0.92,
            label=mode, color=MODE_COLOUR[mode], edgecolor="white", linewidth=1.2,
            yerr=errs, capsize=3, error_kw={"ecolor": INK, "elinewidth": 1.2},
        )
        # Direct labels: required relief for the amber's sub-3:1 contrast, and
        # they make the figure readable without the axis.
        for bar, mean in zip(bars, means, strict=True):
            ax.text(
                bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.035,
                f"{mean:.2f}", ha="center", va="bottom", fontsize=8, color=INK,
            )

    ax.set_xticks(list(positions))
    ax.set_xticklabels([c.name for c in CLASS_ORDER])
    ax.set_xlabel("true access class (netem profile configured)", fontsize=10, color=INK)
    ax.set_ylabel("completion rate (fraction of joined tickets)", fontsize=10, color=INK)
    ax.set_ylim(0, 1.15)
    ax.set_title(
        "Per-class completion rate by mode (mean of "
        f"{len(result.seeds)} seeds, 95% CI)",
        fontsize=11, color=INK, pad=12,
    )
    ax.legend(frameon=False, fontsize=9, ncols=3, loc="upper center",
              bbox_to_anchor=(0.5, -0.16))
    _style(ax)
    return _save(fig, out_dir, "fig1-completion-rate.png")


# --------------------------------------------------------------------------
# figure 2 — Delta across conditions
# --------------------------------------------------------------------------


def fig_delta(result: ExperimentResult, out_dir: Path) -> Path:
    """One Delta per mode with a 95% CI. §4.6 calls this the falsification figure."""
    fig, ax = plt.subplots(figsize=(7, 4.2))
    modes = [m for m in MODE_ORDER if m in result.modes]

    for i, mode in enumerate(modes):
        per_seed = result.delta_series(mode)
        mean, half = _ci(per_seed)
        ax.errorbar(
            i, mean, yerr=half, fmt="o", markersize=9, color=MODE_COLOUR[mode],
            ecolor=MODE_COLOUR[mode], elinewidth=2, capsize=5, capthick=2,
            markeredgecolor="white", markeredgewidth=1.5,
        )
        ax.scatter([i] * len(per_seed), per_seed, s=18, color=MODE_COLOUR[mode],
                   alpha=0.35, zorder=1)
        # Nudged clear of the zero reference line when Delta is ~0.
        offset = 0.14 if abs(mean) > half else 0.22
        ax.annotate(
            f"{mean:.3f}", xy=(i, mean), xytext=(i + offset, mean + half + 0.004),
            fontsize=9, color=INK, va="bottom", ha="left",
        )

    ax.axhline(0.0, color=MUTED, linewidth=1.2, linestyle="--")
    ax.set_xticks(range(len(modes)))
    ax.set_xticklabels(modes)
    ax.set_xlim(-0.5, len(modes) - 0.25)
    ax.set_xlabel("mode", fontsize=10, color=INK)
    ax.set_ylabel("Δ  (completion rate HIGH − LOW)", fontsize=10, color=INK)
    ax.set_title(
        f"Completion gap Δ by mode\n"
        f"mean of {len(result.seeds)} seeds, 95% CI; faint dots are individual seeds",
        fontsize=10.5, color=INK, pad=12,
    )
    _style(ax)
    return _save(fig, out_dir, "fig2-delta.png")


# --------------------------------------------------------------------------
# figure 3 — attempt-count CCDF, LOW class only
# --------------------------------------------------------------------------


def fig_attempt_ccdf(result: ExperimentResult, out_dir: Path) -> Path:
    """P(attempts > k) for the LOW class. Shows the divergent tail collapsing, or not."""
    fig, ax = plt.subplots(figsize=(7, 4.4))
    low = int(AccessClass.LOW)
    drawn = 0

    for mode in (m for m in ("baseline", "aaac") if m in result.modes):
        pooled: dict[int, list[float]] = {}
        for record in result.for_mode(mode):
            for k, p in record.metrics.per_class[low].attempts_ccdf:
                pooled.setdefault(k, []).append(p)
        if not pooled:
            continue
        ks = sorted(pooled)
        ys = [sum(pooled[k]) / len(pooled[k]) for k in ks]
        ax.step(ks, ys, where="post", linewidth=2, color=MODE_COLOUR[mode], label=mode)
        ax.plot(ks, ys, "o", markersize=5, color=MODE_COLOUR[mode],
                markeredgecolor="white", markeredgewidth=1)
        if ys:
            ax.text(ks[-1] + 0.1, ys[-1], mode, fontsize=9, color=INK, va="center")
        drawn += 1

    if drawn == 0:
        ax.text(0.5, 0.5, "no baseline or aaac runs in this result",
                ha="center", va="center", transform=ax.transAxes, color=MUTED)

    ax.set_yscale("symlog", linthresh=1e-3)
    ax.set_xlabel("attempts k (ADMIT events per completed ticket)", fontsize=10, color=INK)
    ax.set_ylabel("P(attempts > k)", fontsize=10, color=INK)
    ax.set_title("Attempt-count CCDF, LOW class only", fontsize=11, color=INK, pad=12)
    if drawn >= 2:
        ax.legend(frameon=False, fontsize=9)
    _style(ax)
    return _save(fig, out_dir, "fig3-attempt-ccdf.png")


# --------------------------------------------------------------------------
# figure 4 — time series for one representative run
# --------------------------------------------------------------------------


def fig_time_series(metrics: RunMetrics, events: Sequence[dict], out_dir: Path) -> Path:
    """Small multiples sharing one time axis — never a dual axis.

    §4.6 asks for alpha, mu-hat, in-flight and origin p99 together. Those are a
    rate, a rate, a count and a latency; forcing them onto two y-scales would
    make their relative movement meaningless. Panels for CONTROL-derived series
    are drawn empty and labelled when M1's controller has not emitted them.
    """
    samples = [e for e in events if e.get("event") == "ORIGIN_SAMPLE"]
    controls = [e for e in events if e.get("event") == "CONTROL"]
    t0 = min((float(e["ts"]) for e in events if "ts" in e), default=0.0)

    panels: list[tuple[str, str, list[float], list[float]]] = [
        (
            "admission rate α",
            "tickets/s",
            [float(e["ts"]) - t0 for e in controls if e.get("alpha") is not None],
            [float(e["alpha"]) for e in controls if e.get("alpha") is not None],
        ),
        (
            "estimated capacity μ̂",
            "req/s",
            [float(e["ts"]) - t0 for e in controls if e.get("mu_hat") is not None],
            [float(e["mu_hat"]) for e in controls if e.get("mu_hat") is not None],
        ),
        (
            "origin in-flight",
            "requests",
            [float(e["ts"]) - t0 for e in samples if e.get("in_flight") is not None],
            [float(e["in_flight"]) for e in samples if e.get("in_flight") is not None],
        ),
        (
            "origin p99 latency",
            "ms",
            [float(e["ts"]) - t0 for e in samples if e.get("p99_ms") is not None],
            [float(e["p99_ms"]) for e in samples if e.get("p99_ms") is not None],
        ),
    ]

    fig, axes = plt.subplots(len(panels), 1, figsize=(8, 8.5), sharex=True)
    for ax, (title, unit, xs, ys) in zip(axes, panels, strict=True):
        if xs:
            # Thin the stroke as sample density rises: a 2px line over thousands
            # of points fills solid and hides the shape. The data is untouched -
            # this is a legibility choice, not smoothing.
            width = 2.0 if len(xs) <= 400 else (1.0 if len(xs) <= 1500 else 0.6)
            ax.plot(
                xs, ys, linewidth=width,
                color=MODE_COLOUR.get(metrics.provenance.mode, INK),
            )
            ax.text(
                0.995, 0.92, f"n={len(xs)}", transform=ax.transAxes,
                ha="right", va="top", fontsize=7.5, color=MUTED,
            )
        else:
            ax.text(
                0.5, 0.5, "not emitted in this run (no CONTROL events)",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=9, color=MUTED,
            )
        ax.set_ylabel(f"{title}\n({unit})", fontsize=9, color=INK)
        _style(ax)
    axes[-1].set_xlabel("seconds since first event", fontsize=10, color=INK)
    fig.suptitle(
        f"Controller and origin over one run "
        f"({metrics.provenance.run_id}, mode={metrics.provenance.mode})",
        fontsize=11, color=INK,
    )
    fig.align_ylabels(axes)
    return _save(fig, out_dir, "fig4-time-series.png")


# --------------------------------------------------------------------------
# figure 5 — classifier confusion matrix
# --------------------------------------------------------------------------


def fig_confusion(metrics: RunMetrics, out_dir: Path) -> Path:
    """Sequential single hue, light to dark. Rows are true_class, columns estimated."""
    confusion = metrics.classifier.confusion
    grid = [[confusion[int(t)][int(e)] for e in CLASS_ORDER] for t in CLASS_ORDER]
    row_totals = [max(1, sum(row)) for row in grid]
    normalised = [[v / total for v in row] for row, total in zip(grid, row_totals, strict=True)]

    fig, ax = plt.subplots(figsize=(5.6, 5.0))
    image = ax.imshow(normalised, cmap=BLUES, vmin=0.0, vmax=1.0)

    for i, row in enumerate(grid):
        for j, count in enumerate(row):
            share = normalised[i][j]
            ax.text(
                j, i, f"{count}\n{share:.1%}", ha="center", va="center", fontsize=9,
                color="white" if share > 0.55 else INK,
            )

    ax.set_xticks(range(len(CLASS_ORDER)))
    ax.set_yticks(range(len(CLASS_ORDER)))
    ax.set_xticklabels([c.name for c in CLASS_ORDER])
    ax.set_yticklabels([c.name for c in CLASS_ORDER])
    ax.set_xlabel("estimated access class (ESTIMATE.access_class)", fontsize=10, color=INK)
    ax.set_ylabel("true access class (netem profile)", fontsize=10, color=INK)

    accuracy = metrics.classifier.accuracy
    optimistic = metrics.classifier.optimistic_error_rate
    subtitle = (
        f"n={metrics.classifier.n}"
        + (f", accuracy {accuracy:.3f}" if accuracy is not None else "")
        + (f", optimistic error {optimistic:.3f}" if optimistic is not None else "")
    )
    ax.set_title(f"Classifier confusion matrix\n{subtitle}", fontsize=10.5, color=INK, pad=12)
    bar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
    bar.set_label("share of true-class row", fontsize=9, color=MUTED)
    bar.ax.tick_params(colors=MUTED, labelsize=8)
    ax.tick_params(colors=MUTED, labelsize=9)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return _save(fig, out_dir, "fig5-confusion-matrix.png")


# --------------------------------------------------------------------------


def render_all(
    result: ExperimentResult,
    out_dir: Path,
    representative: RunMetrics | None = None,
    representative_events: Sequence[dict] | None = None,
) -> list[Path]:
    paths = [
        fig_completion_rate(result, out_dir),
        fig_delta(result, out_dir),
        fig_attempt_ccdf(result, out_dir),
    ]
    if representative is not None:
        paths.append(fig_time_series(representative, representative_events or [], out_dir))
        paths.append(fig_confusion(representative, out_dir))
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate every figure (§4.6)")
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--seeds", default="1,2,3,4,5")
    parser.add_argument("--synthetic", action="store_true", help="FABRICATED DATA")
    args = parser.parse_args(argv)

    from aaac.evaluation.experiment import SyntheticRunner, run_matrix
    from aaac.origin.config import load_run_config

    if not args.synthetic:
        print(
            "plots: no real event logs exist yet — M1's admission service is the single\n"
            "writer of the event log (§3.8) and is not in the repository. Re-run with\n"
            "--synthetic to regenerate the figures from fabricated data.",
        )
        return 2

    load_cfg = load_run_config().require_load()
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    result = run_matrix(seeds, list(MODE_ORDER), SyntheticRunner(), args.results, load_cfg)
    from aaac.evaluation.events import EventLog

    representative = result.for_mode("aaac")[0]
    events = EventLog.read(representative.events_path).events

    paths = render_all(
        result, args.results / "figures", representative.metrics, events
    )
    for path in paths:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
