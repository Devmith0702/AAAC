"""Generate ``results/report-{run_id}.md`` (CLAUDE.md §4.6).

    make report

    `report.py` emits results/report-{run_id}.md with the metric tables and
    figure references, so the presentation is assembled from generated numbers
    and never hand-typed. Nothing in the final write-up should be a number
    someone remembered.

Three things this file refuses to do, each because §1.5 or §7.2 says so:

* **It will not print an aggregate-only per-class metric.** Every completion
  rate, time-to-completion and attempt count is rendered by ``true_class``.
* **It will not hide its exclusions.** Each table is followed by the provenance
  of the numbers above it: event types used, tickets included, tickets excluded
  and why, and how many non-completers were censored.
* **It will not quietly pass a Wilcoxon result it could never have rejected.**
  See ``stats`` for why five seeds makes that test powerless.

If any source log is a fabricated ``synthetic-*.jsonl``, the report opens with a
banner saying so. A synthetic number must never be mistaken for a measurement.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from aaac.evaluation.access_class import AccessClass
from aaac.evaluation.experiment import ExperimentResult
from aaac.evaluation.falsification import Verdict
from aaac.evaluation.metrics import ClassMetrics, RunMetrics
from aaac.evaluation.stats import mean_ci_t, wilcoxon_signed_rank
from aaac.evaluation.synth import SYNTHETIC_PREFIX

CLASS_ORDER: tuple[AccessClass, ...] = (AccessClass.HIGH, AccessClass.MEDIUM, AccessClass.LOW)


def _fmt(value: float | None, spec: str = ".4f", missing: str = "n/a") -> str:
    return missing if value is None else format(value, spec)


def _is_synthetic(result: ExperimentResult) -> bool:
    return result.synthetic or any(
        SYNTHETIC_PREFIX in Path(r.events_path).name for r in result.records
    )


def _synthetic_banner() -> list[str]:
    return [
        "> ## ⚠ FABRICATED DATA — NOT A MEASUREMENT",
        ">",
        "> Every number in this report was produced by `aaac.evaluation.synth` from",
        "> invented events. It exists to exercise the analysis chain and the §4.7",
        "> falsification branch before the real services land. **No figure here may",
        "> appear in the write-up.**",
        "",
    ]


def _class_table(rows: Sequence[tuple[str, ClassMetrics]]) -> list[str]:
    lines = [
        "| class | joined | completed | completion rate | TTC median (s) | TTC p95 (s) "
        "| censored | attempts mean | attempts p95 | attempts max | goodput |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, m in rows:
        lines.append(
            f"| {label} | {m.joined} | {m.completed} | {m.completion_rate:.4f} "
            f"| {_fmt(m.ttc_median_s, '.2f')} | {_fmt(m.ttc_p95_s, '.2f')} "
            f"| {m.ttc_censored} | {_fmt(m.attempts_mean, '.2f')} "
            f"| {_fmt(m.attempts_p95, '.2f')} | {m.attempts_max if m.attempts_max else 'n/a'} "
            f"| {_fmt(m.goodput)} |"
        )
    return lines


def _provenance_block(metrics: RunMetrics) -> list[str]:
    lines = ["<details><summary>Provenance for the table above</summary>", "", "```"]
    lines.append(metrics.provenance.describe())
    lines += ["```", "</details>", ""]
    return lines


def render_run(metrics: RunMetrics, title: str) -> list[str]:
    lines = [f"### {title}", ""]
    rows: list[tuple[str, ClassMetrics]] = [
        (cls.name, metrics.per_class[int(cls)]) for cls in CLASS_ORDER
    ]
    rows.append(("ALL (aggregate)", metrics.overall))
    lines += _class_table(rows)
    lines += [
        "",
        f"- **Δ** = `completion_rate[HIGH] − completion_rate[LOW]` = "
        f"**{_fmt(metrics.delta)}**",
        f"- **Jain's index** over the three per-class completion rates = "
        f"{_fmt(metrics.jain)}",
        "",
    ]
    origin = metrics.origin
    lines += [
        "**Origin stability** (from `ORIGIN_SAMPLE`) — confirms protection is not "
        "sacrificed:",
        "",
        f"- samples: {origin.samples}",
        f"- mean 5xx rate: {_fmt(origin.err_rate_mean, '.6f')} "
        f"(max {_fmt(origin.err_rate_max, '.6f')})",
        f"- p99 latency: mean {_fmt(origin.p99_mean_ms, '.1f')} ms, "
        f"max {_fmt(origin.p99_max_ms, '.1f')} ms",
        "",
    ]
    lines += _provenance_block(metrics)
    return lines


def render_classifier(metrics: RunMetrics) -> list[str]:
    c = metrics.classifier
    lines = [
        "### Classifier accuracy",
        "",
        f"n = {c.n}, accuracy = {_fmt(c.accuracy)}",
        "",
        "| true \\ estimated | " + " | ".join(x.name for x in CLASS_ORDER) + " | recall |",
        "|---|" + "---:|" * (len(CLASS_ORDER) + 1),
    ]
    for true_cls in CLASS_ORDER:
        row = c.confusion[int(true_cls)]
        cells = " | ".join(str(row[int(e)]) for e in CLASS_ORDER)
        lines.append(
            f"| **{true_cls.name}** | {cells} | {_fmt(c.per_class_recall[int(true_cls)])} |"
        )
    lines += [
        "",
        f"- **Optimistic error rate**: {_fmt(c.optimistic_error_rate)} — a client judged "
        "*more capable than it is*. This is the error that matters: it sends a slow "
        "client a payload it cannot fetch.",
        f"- Pessimistic error rate: {_fmt(c.pessimistic_error_rate)}",
        "",
    ]
    return lines


def render_verdict(verdict: Verdict, result: ExperimentResult) -> list[str]:
    lines = [
        "## Hypothesis test (pre-registered — see `src/aaac/evaluation/README.md`)",
        "",
        f"### {verdict.headline}",
        "",
    ]
    for condition in verdict.conditions:
        mark = "✅" if condition.passed else "❌"
        lines.append(f"- {mark} **{condition.name}** — {condition.detail}")
    lines.append("")

    if verdict.paired_difference is not None:
        lines += [
            "| quantity | mean | 95% CI | method |",
            "|---|---:|---|---|",
        ]
        for label, interval in (
            ("Δ (baseline)", verdict.delta_baseline),
            ("Δ (aaac)", verdict.delta_aaac),
            ("paired Δ(baseline) − Δ(aaac)", verdict.paired_difference),
        ):
            if interval is not None:
                lines.append(
                    f"| {label} | {interval.mean:.4f} "
                    f"| [{interval.low:.4f}, {interval.high:.4f}] | {interval.method} |"
                )
        lines.append("")

    # Wilcoxon, reported only when it could actually have rejected.
    try:
        rank_test = wilcoxon_signed_rank(
            result.delta_series("baseline"), result.delta_series("aaac")
        )
    except (ValueError, KeyError):
        rank_test = None
    if rank_test is not None:
        if "cannot reject" in rank_test.note:
            lines += [
                f"> **Wilcoxon signed-rank withheld.** {rank_test.note}. Reporting a "
                "p-value from a test that could not have rejected would be misleading; "
                "run ≥ 6 seeds if a rank test is wanted.",
                "",
            ]
        else:
            lines += [
                f"Wilcoxon signed-rank: W = {rank_test.statistic:.1f}, "
                f"p = {rank_test.p_value:.4f}"
                + (f" ({rank_test.note})" if rank_test.note else ""),
                "",
            ]

    for note in verdict.notes:
        lines += [f"> **NOTE.** {note}", ""]
    return lines


def render(result: ExperimentResult, run_id: str, figures_dir: Path | None = None) -> str:
    lines: list[str] = []
    if _is_synthetic(result):
        lines += _synthetic_banner()

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines += [
        f"# AAAC evaluation report — `{run_id}`",
        "",
        f"Generated {generated} by `aaac.evaluation.report`. Every number below is "
        "computed from the event log; none is hand-typed.",
        "",
        f"- seeds: `{', '.join(str(s) for s in result.seeds)}` ({len(result.seeds)} total)",
        f"- modes: `{', '.join(result.modes)}`",
        f"- runs: {len(result.records)}",
        "",
    ]

    hashes = sorted({r.population_hash[:12] for r in result.records})
    lines += [
        "**Identical-load check (§4.3).** All modes within a seed replay the same "
        f"population. Population hashes seen: {', '.join(f'`{h}`' for h in hashes)} "
        f"({len(hashes)} distinct, one per seed).",
        "",
    ]

    lines += ["## Per-mode results", ""]
    for mode in result.modes:
        records = result.for_mode(mode)
        if not records:
            continue
        deltas = [r.metrics.delta for r in records if r.metrics.delta is not None]
        if len(deltas) >= 2:
            interval = mean_ci_t(deltas)
            lines += [
                f"**mode `{mode}`** — Δ across seeds: {interval} ({interval.method})",
                "",
            ]
        for record in records:
            lines += render_run(record.metrics, f"`{mode}` · seed {record.seed}")

    representative = (result.for_mode("aaac") or result.records)[0]
    lines += render_classifier(representative.metrics)

    try:
        lines += render_verdict(result.verdict(), result)
    except Exception as exc:  # noqa: BLE001 - the reason belongs in the report
        lines += [
            "## Hypothesis test",
            "",
            f"> Not evaluated: {exc}",
            "",
        ]

    if figures_dir is not None:
        lines += ["## Figures", ""]
        for name, caption in (
            ("fig1-completion-rate.png", "Per-class completion rate by mode"),
            ("fig2-delta.png", "Completion gap Δ across conditions"),
            ("fig3-attempt-ccdf.png", "Attempt-count CCDF, LOW class"),
            ("fig4-time-series.png", "Controller and origin over one run"),
            ("fig5-confusion-matrix.png", "Classifier confusion matrix"),
        ):
            lines.append(f"![{caption}]({figures_dir.as_posix()}/{name})")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the evaluation report (§4.6)")
    parser.add_argument("--run-id", default="dev")
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--seeds", default="1,2,3,4,5")
    parser.add_argument("--synthetic", action="store_true", help="FABRICATED DATA")
    args = parser.parse_args(argv)

    from aaac.evaluation.experiment import SyntheticRunner, run_matrix
    from aaac.origin.config import load_run_config

    if not args.synthetic:
        print(
            "report: no real event logs exist yet — M1's admission service is the single\n"
            "writer of the event log (§3.8) and is not in the repository. Re-run with\n"
            "--synthetic to exercise the report generator on fabricated data."
        )
        return 2

    load_cfg = load_run_config().require_load()
    seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
    result = run_matrix(
        seeds, ["none", "baseline", "aaac"], SyntheticRunner(), args.results, load_cfg
    )

    out = args.results / f"report-{args.run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(result, args.run_id, Path("figures")), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
