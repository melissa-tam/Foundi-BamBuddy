/**
 * Structural pin: the app-wide prompts have ONE owner, `components/GlobalPrompts`, which
 * the main `Layout` and the touch view's `TouchLayout` both mount. A second caller of a
 * prompt hook would raise every spool prompt twice (two toasts, two dialogs); a second
 * `plate-not-empty` listener, two plate alerts.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(path.join(srcDir, dir), { withFileTypes: true })) {
      const rel = dir ? `${dir}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (entry.name !== '__tests__') walk(rel);
      } else if (/\.tsx?$/.test(entry.name)) out.push(rel);
    }
  };
  walk('');
  return out;
}

const read = (rel: string): string => fs.readFileSync(path.join(srcDir, rel), 'utf8');

/** A call of the hook (not its definition, not an import). */
const CALLS: ReadonlyArray<[string, RegExp]> = [
  ['useUnknownTagPrompt', /(?<!function\s)\buseUnknownTagPrompt\(\)/],
  ['useRespoolPrompt', /(?<!function\s)\buseRespoolPrompt\(\)/],
  ['useTaglessFreshPrompt', /(?<!function\s)\buseTaglessFreshPrompt\(\)/],
  ['usePendingPromptSync', /(?<!function\s)\busePendingPromptSync\(\)/],
  ['the plate-not-empty listener', /addEventListener\(\s*['"]plate-not-empty['"]/],
];

describe('the global prompts have one owner', () => {
  it.each(CALLS)('%s appears only in GlobalPrompts', (_label, pattern) => {
    const owners = sourceFiles().filter((file) => pattern.test(read(file)));
    expect(owners).toEqual(['components/GlobalPrompts.tsx']);
  });

  it('replays durable prompts AFTER the prompt hooks it feeds', () => {
    const src = read('components/GlobalPrompts.tsx');
    const sync = src.indexOf('usePendingPromptSync();');
    for (const hook of ['useUnknownTagPrompt()', 'useRespoolPrompt()', 'useTaglessFreshPrompt()']) {
      expect(src.indexOf(hook)).toBeGreaterThan(-1);
      expect(src.indexOf(hook)).toBeLessThan(sync);
    }
  });

  it('is mounted by both shells', () => {
    expect(read('components/Layout.tsx')).toMatch(/<GlobalPrompts\s*\/>/);
    expect(read('components/touch/TouchLayout.tsx')).toMatch(/<GlobalPrompts\s*\/>/);
  });
});
