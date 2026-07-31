import { Pipe, PipeTransform } from '@angular/core';

/**
 * Formats a cell value for display.
 *
 * Deliberately does not round: the answer text quotes exact figures, and a
 * table that disagreed with the prose beside it would undermine the point of
 * showing both.
 */
@Pipe({ name: 'cellValue', standalone: true })
export class CellValuePipe implements PipeTransform {
  transform(value: unknown): string {
    if (value === null || value === undefined || value === '') {
      return '—';
    }
    if (typeof value === 'boolean') {
      return value ? 'Yes' : 'No';
    }
    if (typeof value === 'number') {
      return Number.isInteger(value)
        ? value.toLocaleString('en-US')
        : value.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          });
    }
    return String(value);
  }
}

/** Abbreviates large amounts for axis labels, where full digits are unreadable. */
@Pipe({ name: 'compactAmount', standalone: true })
export class CompactAmountPipe implements PipeTransform {
  transform(value: number | null | undefined): string {
    if (value === null || value === undefined || Number.isNaN(value)) {
      return '—';
    }
    const abs = Math.abs(value);
    if (abs >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
    return `$${value.toFixed(2)}`;
  }
}
