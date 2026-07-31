import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { CellValuePipe } from '../shared/format.pipes';

/** Supporting rows for an answer (FR-010). */
@Component({
  selector: 'app-result-table',
  standalone: true,
  imports: [CellValuePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (columns().length) {
      <div class="wrap">
        <table>
          <thead>
            <tr>
              @for (column of columns(); track column) {
                <th>{{ label(column) }}</th>
              }
            </tr>
          </thead>
          <tbody>
            @for (row of visible(); track $index) {
              <tr>
                @for (column of columns(); track column) {
                  <td [class.numeric]="isNumeric(row[column])">{{ row[column] | cellValue }}</td>
                }
              </tr>
            }
          </tbody>
        </table>
        @if (rows().length > limit) {
          <p class="more">Showing {{ limit }} of {{ rows().length }} rows.</p>
        }
      </div>
    }
  `,
  styles: [
    `
      .wrap {
        overflow-x: auto;
        margin-top: 0.75rem;
        border: 1px solid #e2e5ea;
        border-radius: 8px;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 0.85rem;
      }
      th,
      td {
        padding: 0.5rem 0.75rem;
        text-align: left;
        border-bottom: 1px solid #eef0f3;
        white-space: nowrap;
      }
      th {
        background: #f7f8fa;
        font-weight: 600;
        position: sticky;
        top: 0;
      }
      td.numeric {
        text-align: right;
        font-variant-numeric: tabular-nums;
      }
      tbody tr:last-child td {
        border-bottom: none;
      }
      .more {
        margin: 0;
        padding: 0.5rem 0.75rem;
        color: #6b7280;
        font-size: 0.78rem;
        background: #f7f8fa;
      }
    `,
  ],
})
export class ResultTableComponent {
  readonly rows = input.required<Record<string, unknown>[]>();
  readonly limit = 50;

  readonly columns = computed(() => {
    const rows = this.rows();
    return rows.length ? Object.keys(rows[0]) : [];
  });

  readonly visible = computed(() => this.rows().slice(0, this.limit));

  label(column: string): string {
    if (column === '_id') {
      return 'Group';
    }
    return column.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  isNumeric(value: unknown): boolean {
    return typeof value === 'number';
  }

  format(value: unknown): string {
    if (value === null || value === undefined) {
      return '—';
    }
    if (typeof value === 'number') {
      // Large values in this dataset are money; integers are counts.
      return Number.isInteger(value) && Math.abs(value) < 1_000_000
        ? value.toLocaleString('en-US')
        : value.toLocaleString('en-US', { maximumFractionDigits: 2 });
    }
    return String(value);
  }
}
