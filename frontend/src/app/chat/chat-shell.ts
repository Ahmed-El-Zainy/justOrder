import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ChatService } from '../core/chat.service';
import { PHASE_LABEL } from '../core/models';
import { PipelinePanelComponent } from '../insight/pipeline-panel';
import { ResultChartComponent } from '../insight/result-chart';
import { ResultTableComponent } from '../insight/result-table';

@Component({
  selector: 'app-chat-shell',
  standalone: true,
  imports: [FormsModule, ResultTableComponent, ResultChartComponent, PipelinePanelComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './chat-shell.html',
  styleUrl: './chat-shell.scss',
})
export class ChatShellComponent implements OnInit {
  private readonly chat = inject(ChatService);

  readonly messages = this.chat.messages;
  readonly busy = this.chat.busy;
  readonly suggestions = this.chat.suggestions;
  readonly draft = signal('');

  ngOnInit(): void {
    void this.chat.loadSuggestions();
  }

  phaseLabel(phase: string | undefined): string {
    return phase ? (PHASE_LABEL[phase as keyof typeof PHASE_LABEL] ?? 'Working') : 'Working';
  }

  send(): void {
    const question = this.draft().trim();
    if (!question || this.busy()) {
      return;
    }
    this.draft.set('');
    void this.chat.ask(question);
  }

  useSuggestion(text: string): void {
    if (this.busy()) {
      return;
    }
    void this.chat.ask(text);
  }

  onEnter(event: Event): void {
    const keyboard = event as KeyboardEvent;
    if (!keyboard.shiftKey) {
      event.preventDefault();
      this.send();
    }
  }
}
