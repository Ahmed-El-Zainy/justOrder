import { ChangeDetectionStrategy, Component } from '@angular/core';
import { ChatShellComponent } from './chat/chat-shell';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [ChatShellComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: '<app-chat-shell />',
})
export class App {}
