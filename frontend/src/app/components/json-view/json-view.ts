import { Component, computed, input } from '@angular/core';

const TOKEN = /("(?:\\.|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/** Pretty-prints a value as colour-coded JSON. The text is escaped before any markup is added. */
@Component({
  selector: 'app-json-view',
  templateUrl: './json-view.html',
  styleUrl: './json-view.scss',
})
export class JsonView {
  value = input.required<unknown>();

  /** the plain text, e.g. for a "copy" button */
  text = computed(() => JSON.stringify(this.value(), null, 2));

  html = computed(() => {
    const escaped = this.text().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return escaped.replace(TOKEN, (match, _all, colon) => {
      const kind = colon ? 'key' : match.startsWith('"') ? 'str' : /true|false/.test(match) ? 'bool' : match === 'null' ? 'null' : 'num';
      return `<span class="${kind}">${match}</span>`;
    });
  });
}
