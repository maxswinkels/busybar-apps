import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);

let cache: Promise<Map<string, string>> | null = null;

/**
 * When each app folder first appeared, read from git history so nobody has to
 * maintain a date field in the manifest by hand. Needs a full clone: the
 * workflows check out with `fetch-depth: 0` for this. Without history every
 * app comes back undefined and "Newest first" falls back to alphabetical.
 */
export function getAddedDates(): Promise<Map<string, string>> {
  cache ??= load();
  return cache;
}

async function load(): Promise<Map<string, string>> {
  const dates = new Map<string, string>();

  let stdout: string;
  try {
    ({ stdout } = await run(
      'git',
      ['log', '--reverse', '--diff-filter=AR', '--format=%aI', '--name-status', '--', 'apps/'],
      { maxBuffer: 32 * 1024 * 1024 }
    ));
  } catch {
    console.warn('[gallery] no git history available, "Newest first" falls back to A-Z');
    return dates;
  }

  // Output alternates: a commit date on its own line, then one tab-separated
  // status line per path it touched.
  let date = '';
  for (const line of stdout.split('\n')) {
    if (!line) continue;

    const [status, from, to] = line.split('\t');
    if (to === undefined && from === undefined) {
      date = line;
      continue;
    }

    if (status.startsWith('R')) {
      // A renamed folder (apps/weather_forecast -> apps/weather-forecast) keeps
      // the date it had under its old name instead of looking brand new.
      const oldSlug = slugOf(from);
      const newSlug = slugOf(to);
      const original = oldSlug && dates.get(oldSlug);
      if (newSlug && original && !dates.has(newSlug)) dates.set(newSlug, original);
      continue;
    }

    // --reverse means the first time we see a folder is when it was added.
    const slug = slugOf(from);
    if (slug && !dates.has(slug)) dates.set(slug, date);
  }

  return dates;
}

function slugOf(path: string | undefined): string | undefined {
  return path?.startsWith('apps/') ? path.split('/')[1] : undefined;
}
