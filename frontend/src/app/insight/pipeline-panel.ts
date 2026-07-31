import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { Derivation } from '../core/models';

/**
 * How the answer was derived (FR-021, constitution Principle VI).
 *
 * Showing the executed pipeline is what lets a reader check the assistant's
 * interpretation instead of trusting the number.
 */
@Component({
  selector: 'app-pipeline-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="panel">
      <button type="button" (click)="open.set(!open())">
        <span class="chevron" [class.rotated]="open()">›</span>
        How this was answered
        <span class="stats">
          {{ derivation().rowCount }} row{{ derivation().rowCount === 1 ? '' : 's' }} ·
          {{ derivation().elapsedMs }} ms
          @if (derivation().attempts > 1) {
            · {{ derivation().attempts }} attempts
          }
          @if (derivation().truncated) {
            · truncated
          }
        </span>
      </button>

      @if (open()) {
        <pre>{{ pretty() }}</pre>
      }
    </div>
  `,
  styles: [
    `
      .panel {
        margin-top: 0.75rem;
        border: 1px solid #e2e5ea;
        border-radius: 8px;
        overflow: hidden;
      }
      button {
        width: 100%;
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.5rem 0.75rem;
        background: #f7f8fa;
        border: none;
        cursor: pointer;
        font-size: 0.8rem;
        color: #374151;
        text-align: left;
      }
      button:hover {
        background: #eef0f3;
      }
      .chevron {
        display: inline-block;
        transition: transform 0.15s;
        font-size: 1rem;
      }
      .chevron.rotated {
        transform: rotate(90deg);
      }
      .stats {
        margin-left: auto;
        color: #6b7280;
        font-variant-numeric: tabular-nums;
      }
      pre {
        margin: 0;
        padding: 0.75rem;
        background: #1f2430;
        color: #d6deeb;
        font-size: 0.75rem;
        overflow-x: auto;
        line-height: 1.5;
      }
    `,
  ],
})
export class PipelinePanelComponent {
  readonly derivation = input.required<Derivation>();
  readonly open = signal(false);

  readonly pretty = computed(() => JSON.stringify(this.derivation().pipeline, null, 2));
}
