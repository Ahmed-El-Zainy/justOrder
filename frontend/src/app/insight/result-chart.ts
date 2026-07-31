import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { BaseChartDirective } from 'ng2-charts';
import { ChartConfiguration, ChartType } from 'chart.js';
import { ChartSpec } from '../core/models';

/**
 * Charts a ranked comparison or a time series (FR-022).
 *
 * Scalars are never charted — a single figure gains nothing from a bar, and the
 * backend withholds a chart spec for them.
 */
@Component({
  selector: 'app-result-chart',
  standalone: true,
  imports: [BaseChartDirective],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (data().labels?.length) {
      <div class="chart">
        <canvas baseChart [type]="chartType()" [data]="data()" [options]="options()"></canvas>
      </div>
    }
  `,
  styles: [
    `
      .chart {
        margin-top: 0.75rem;
        padding: 0.75rem;
        border: 1px solid #e2e5ea;
        border-radius: 8px;
        height: 280px;
        position: relative;
      }
    `,
  ],
})
export class ResultChartComponent {
  readonly spec = input.required<ChartSpec>();
  readonly rows = input.required<Record<string, unknown>[]>();

  readonly chartType = computed<ChartType>(() => this.spec().type);

  readonly data = computed<ChartConfiguration['data']>(() => {
    const { x_field, y_field } = this.spec();
    // A chart with 90 bars is unreadable; the table beneath carries the rest.
    const rows = this.rows().slice(0, 15);

    return {
      labels: rows.map((row) => this.label(row[x_field])),
      datasets: [
        {
          data: rows.map((row) => Number(row[y_field] ?? 0)),
          label: this.pretty(y_field),
          backgroundColor: '#2563eb',
          borderColor: '#2563eb',
          borderWidth: this.spec().type === 'line' ? 2 : 0,
          fill: false,
          tension: 0.25,
          pointRadius: 3,
        },
      ],
    };
  });

  readonly options = computed<ChartConfiguration['options']>(() => ({
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      title: { display: !!this.spec().title, text: this.spec().title, font: { size: 12 } },
      tooltip: {
        callbacks: {
          label: (context) => ` ${this.compact(context.parsed.y ?? 0)}`,
        },
      },
    },
    scales: {
      x: { ticks: { autoSkip: false, maxRotation: 55, minRotation: 0, font: { size: 10 } } },
      y: { ticks: { callback: (value) => this.compact(Number(value)) } },
    },
  }));

  private label(value: unknown): string {
    if (value === null || value === undefined || value === '') {
      return '(none)';
    }
    const text = String(value);
    return text.length > 28 ? `${text.slice(0, 27)}…` : text;
  }

  private pretty(field: string): string {
    return field.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  /** Procurement totals run to billions; full digits make the axis unreadable. */
  private compact(value: number): string {
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
    return value.toLocaleString('en-US');
  }
}
