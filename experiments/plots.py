"""Publication-quality charts from measured experiment results.

Run (after run_experiment.py):
    python -m experiments.plots

Reads experiments/results/summary.csv. It will not invent data: if the results
file is missing the script tells you to run the experiment first rather than
producing a plot of nothing.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

RESULTS = Path("experiments/results")

PANELS = [
    ("completion_rate", "Transaction completion rate", "fraction of trials resolved"),
    ("manual_intervention_rate", "Manual intervention rate", "fraction requiring a human"),
    ("avg_api_calls", "Cross-organization API calls", "mean calls per transaction"),
    ("duplicate_side_effects_total", "Duplicate business operations",
     "extra reservations/bookings observed at the organizations"),
]


def load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"{path} not found.\n"
            "Run the experiment first:  python -m experiments.run_experiment"
        )
    with path.open() as fh:
        return list(csv.DictReader(fh))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plot experiment results")
    parser.add_argument("--summary", default=str(RESULTS / "summary.csv"))
    parser.add_argument("--outdir", default=str(RESULTS))
    args = parser.parse_args(argv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = load(Path(args.summary))
    rates = sorted({float(r["failure_rate"]) for r in rows})

    def series(system: str, key: str) -> list[float]:
        out = []
        for f in rates:
            match = [r for r in rows
                     if r["system"] == system and float(r["failure_rate"]) == f]
            out.append(float(match[0][key]) if match else 0.0)
        return out

    plt.rcParams.update({
        "figure.dpi": 150, "font.size": 9, "axes.grid": True,
        "grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False,
    })

    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    x = range(len(rates))
    width = 0.36

    for ax, (key, title, ylabel) in zip(axes.flat, PANELS, strict=True):
        b = series("baseline", key)
        s = series("servicemesh", key)
        ax.bar([i - width / 2 for i in x], b, width,
               label="Sequential baseline", color="#c1614d")
        ax.bar([i + width / 2 for i in x], s, width,
               label="ServiceMesh", color="#3d6f9e")
        ax.set_title(title, fontsize=10, fontweight="bold")
        ax.set_ylabel(ylabel, fontsize=8)
        ax.set_xlabel("injected failure rate")
        ax.set_xticks(list(x))
        ax.set_xticklabels([f"{f:.0%}" for f in rates])

    axes.flat[0].legend(fontsize=8, loc="best")
    fig.suptitle(
        "ServiceMesh vs sequential point-to-point integration\n"
        "SYNTHETIC workload — measures this implementation, not industry data",
        fontsize=11, fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(outdir / f"comparison.{ext}", bbox_inches="tight")
    print(f"wrote {outdir}/comparison.png and .pdf")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
