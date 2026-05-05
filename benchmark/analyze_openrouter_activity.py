#!/usr/bin/env python3
"""Summarize exported OpenRouter activity CSVs for model-routing decisions."""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean


def _number(row: dict[str, str], key: str) -> float:
    value = row.get(key) or ""
    return float(value) if value else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[int((len(ordered) - 1) * p)]


def _model_family(model: str) -> str:
    return model.split("-2026", 1)[0]


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", errors="replace") as f:
        return list(csv.DictReader(f))


def summarize(rows: list[dict[str, str]], *, runaway_reasoning_threshold: int) -> str:
    by_model: dict[str, list[dict[str, str]]] = defaultdict(list)
    by_provider: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    by_hour: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    finish_reasons: Counter[str] = Counter()

    for row in rows:
        model = _model_family(row.get("model_permaslug", ""))
        provider = row.get("provider_name", "")
        by_model[model].append(row)
        by_provider[(model, provider)].append(row)
        finish_reasons[row.get("finish_reason_normalized", "")] += 1
        created = row.get("created_at")
        if created:
            dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S.%f")
            by_hour[dt.strftime("%Y-%m-%d %H:00")][model].append(row)

    total_cost = sum(_number(row, "cost_total") for row in rows)
    lines = [
        f"rows: {len(rows)}",
        f"total_cost_usd: {total_cost:.6f}",
        "",
        "by_model:",
        "model\tcalls\tcost_usd\tprompt\tcompletion\treasoning\tavg_cost\tp95_cost\tavg_ms\tp95_ms\trunaway_calls\trunaway_cost",
    ]
    for model, model_rows in sorted(
        by_model.items(),
        key=lambda item: -sum(_number(row, "cost_total") for row in item[1]),
    ):
        costs = [_number(row, "cost_total") for row in model_rows]
        latencies = [_number(row, "generation_time_ms") for row in model_rows if _number(row, "generation_time_ms")]
        runaway = [row for row in model_rows if _number(row, "tokens_reasoning") >= runaway_reasoning_threshold]
        lines.append(
            "\t".join(
                [
                    model,
                    str(len(model_rows)),
                    f"{sum(costs):.6f}",
                    str(int(sum(_number(row, "tokens_prompt") for row in model_rows))),
                    str(int(sum(_number(row, "tokens_completion") for row in model_rows))),
                    str(int(sum(_number(row, "tokens_reasoning") for row in model_rows))),
                    f"{mean(costs):.6f}",
                    f"{_percentile(costs, 0.95):.6f}",
                    f"{mean(latencies) if latencies else 0:.0f}",
                    f"{_percentile(latencies, 0.95):.0f}",
                    str(len(runaway)),
                    f"{sum(_number(row, 'cost_total') for row in runaway):.6f}",
                ]
            )
        )

    lines.extend(["", "by_provider:"])
    for (model, provider), provider_rows in sorted(
        by_provider.items(),
        key=lambda item: (item[0][0], -sum(_number(row, "cost_total") for row in item[1])),
    ):
        lines.append(
            f"{model}\t{provider}\t{len(provider_rows)}\t"
            f"${sum(_number(row, 'cost_total') for row in provider_rows):.4f}"
        )

    lines.extend(["", "hourly:"])
    for hour in sorted(by_hour):
        parts = []
        for model, hour_rows in sorted(
            by_hour[hour].items(),
            key=lambda item: -sum(_number(row, "cost_total") for row in item[1]),
        ):
            parts.append(f"{model}:{len(hour_rows)}/${sum(_number(row, 'cost_total') for row in hour_rows):.3f}")
        lines.append(f"{hour}\t" + " | ".join(parts))

    lines.extend(["", "finish_reasons:"])
    for reason, count in finish_reasons.most_common():
        lines.append(f"{reason or '<blank>'}\t{count}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze OpenRouter activity CSV for HOA discovery model choices")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--runaway-reasoning-threshold", type=int, default=80_000)
    args = parser.parse_args()
    print(summarize(load_rows(args.csv_path), runaway_reasoning_threshold=args.runaway_reasoning_threshold), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
