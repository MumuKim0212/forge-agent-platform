import { useId } from "react";

import type { DailyBucket } from "../../types/api";
import { Table } from "../../components/ui/Table";
import { CHART, DASHBOARD_WORDING, chartGeometry, chartSummary, dailyRows } from "./costDashboard";

/**
 * Daily spend as an inline SVG, with
 * the same figures in a table directly under it. No chart library: the bars are plain
 * rectangles whose geometry comes from `chartGeometry`.
 *
 * Each bar is split in two: the spend of FAILED builds at the bottom (rose) and the
 * rest above it (indigo), so money that went to failures is visible in the day it was
 * spent. The legend names both colours, and the table gives the failed part as a number,
 * so colour is never the only carrier.
 *
 * The SVG is one image to assistive technology (`role="img"`, named and described); the
 * table is how those figures are read without seeing it.
 */
export function DailySpendChart({ days }: { days: DailyBucket[] }) {
  const id = useId();
  const titleId = `${id}-title`;
  const descId = `${id}-desc`;
  const geometry = chartGeometry(days);
  const rows = dailyRows(days);
  const plotRight = CHART.width - CHART.right;

  return (
    <div>
      <svg
        viewBox={`0 0 ${CHART.width} ${CHART.height}`}
        role="img"
        aria-labelledby={titleId}
        aria-describedby={descId}
        className="mt-3 h-auto w-full max-w-3xl"
      >
        <title id={titleId}>{DASHBOARD_WORDING.dailyTitle}</title>
        <desc id={descId}>{chartSummary(days)}</desc>

        {geometry.ticks.map((tick) => (
          <g key={tick.label}>
            <line
              x1={CHART.left}
              x2={plotRight}
              y1={tick.y}
              y2={tick.y}
              className="stroke-slate-200 dark:stroke-slate-700"
              strokeWidth={1}
            />
            <text x={CHART.left - 6} y={tick.y + 4} textAnchor="end" className="fill-slate-600 dark:fill-slate-400 text-[10px]">
              {tick.label}
            </text>
          </g>
        ))}

        {geometry.bars.map((bar) => (
          <g key={bar.day}>
            {bar.otherHeight > 0 && (
              <rect x={bar.x} y={bar.otherY} width={bar.width} height={bar.otherHeight} className="fill-indigo-500 dark:fill-indigo-400" />
            )}
            {bar.failedHeight > 0 && (
              <rect x={bar.x} y={bar.failedY} width={bar.width} height={bar.failedHeight} className="fill-rose-500" />
            )}
            {bar.hasUnmeasured && (
              <rect
                x={bar.x}
                y={geometry.baselineY - 3}
                width={bar.width}
                height={3}
                className="fill-amber-600 dark:fill-amber-400"
              />
            )}
            {bar.showLabel && (
              <text
                x={bar.x + bar.width / 2}
                y={geometry.baselineY + 16}
                textAnchor="middle"
                className="fill-slate-600 dark:fill-slate-400 text-[10px]"
              >
                {bar.label}
              </text>
            )}
          </g>
        ))}
      </svg>

      <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-400">
        <li className="flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-sm bg-indigo-500 dark:bg-indigo-400" />
          {DASHBOARD_WORDING.otherLegend}
        </li>
        <li className="flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-sm bg-rose-500" />
          {DASHBOARD_WORDING.failedLegend}
        </li>
        <li className="flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block h-2.5 w-2.5 rounded-sm bg-amber-600 dark:bg-amber-400" />
          {DASHBOARD_WORDING.unmeasuredMarker}
        </li>
      </ul>

      <details className="mt-2">
        <summary className="cursor-pointer text-sm text-accent dark:text-indigo-400">{DASHBOARD_WORDING.tableToggle}</summary>
        <Table className="mt-2">
          <caption className="sr-only">{DASHBOARD_WORDING.dailyTitle}</caption>
          <Table.Head>
            <Table.Row>
              <Table.HeaderCell scope="col">날짜</Table.HeaderCell>
              <Table.HeaderCell scope="col">지출</Table.HeaderCell>
              <Table.HeaderCell scope="col">그중 실패한 빌드</Table.HeaderCell>
              <Table.HeaderCell scope="col">빌드</Table.HeaderCell>
            </Table.Row>
          </Table.Head>
          <Table.Body>
            {rows.map((row) => (
              <Table.Row key={row.day}>
                <Table.Cell>{row.day}</Table.Cell>
                <Table.Cell>{row.spend}</Table.Cell>
                <Table.Cell>{row.failedSpend ?? "—"}</Table.Cell>
                <Table.Cell>{row.builds}</Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table>
      </details>
    </div>
  );
}
