/**
 * Structural pin: the printer verbs the Printers page card and the touch tile share are
 * defined ONCE, in `hooks/usePrinterActions`, and their prompts once, in
 * `components/PrinterActionPrompts`. A mutation over one of these verbs anywhere else is
 * a second implementation — except the surfaces that kept their own copy on purpose,
 * allowlisted by name (plan F16): SpoolBuddy's clear plate, the camera viewers' chamber
 * light, and the HMS modal's generic dialog-button press.
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

const OWNER = 'hooks/usePrinterActions.ts';

/** Each shared verb's api call, and the files allowed a mutation of it besides the owner. */
const VERBS: ReadonlyArray<[string, readonly string[]]> = [
  ['stopPrint', []],
  ['pausePrint', []],
  ['resumePrint', []],
  ['clearPlate', ['pages/spoolbuddy/SpoolBuddyDashboard.tsx']],
  ['recoverPrinter', []],
  ['setFanSpeed', []],
  ['setChamberLight', ['pages/CameraPage.tsx', 'components/EmbeddedCameraViewer.tsx']],
  ['enterServiceHold', []],
  ['exitServiceHold', []],
  ['rescheduleServiceHold', []],
];

/** A `useMutation` whose `mutationFn` calls `api.<verb>(`. */
function definesMutation(source: string, verb: string): boolean {
  return new RegExp(`mutationFn:[^;]*?api\\.${verb}\\(`, 's').test(source);
}

describe('the shared printer verbs have one owner', () => {
  it.each(VERBS)('a %s mutation lives only in usePrinterActions (or an allowlisted surface)', (verb, allowed) => {
    const definers = sourceFiles().filter((file) => definesMutation(read(file), verb));
    expect(definers).toContain(OWNER);
    expect(definers.filter((file) => file !== OWNER).sort()).toEqual([...allowed].sort());
  });

  it("presses the plate check's Ignore and resume only from the hook", () => {
    const pressers = sourceFiles().filter((file) => /action:\s*'IGNORE_RESUME'/.test(read(file)));
    expect(pressers).toEqual([OWNER]);
  });

  it('owns the card verbs no other surface defines', () => {
    const card = read('pages/PrintersPage.tsx');
    expect(card).toMatch(/usePrinterActions\(printer, status, timeFormat\)/);
    for (const name of ['stopPrintMutation', 'pausePrintMutation', 'resumePrintMutation', 'clearPlateMutation', 'recoverMutation', 'fanSpeedMutation', 'enterServiceHoldMutation']) {
      expect(card).not.toContain(name);
    }
  });

  it('renders the shared prompts from one component on both surfaces', () => {
    expect(read('pages/PrintersPage.tsx')).toMatch(/<PrinterActionPrompts/);
    expect(read('components/touch/TouchTile.tsx')).toMatch(/<PrinterActionPrompts/);
    const confirms = sourceFiles().filter((file) => /t\('printers\.confirm\.stopTitle'\)/.test(read(file)));
    expect(confirms).toEqual(['components/PrinterActionPrompts.tsx']);
  });
});
