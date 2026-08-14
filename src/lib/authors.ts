import { getCollection, type CollectionEntry } from 'astro:content';

export interface Author {
  /** Lowercased handle, used in URLs. */
  slug: string;
  /** Handle exactly as written in the manifest. */
  name: string;
  apps: CollectionEntry<'apps'>[];
}

/**
 * GitHub handles come in mixed casing (LuisWollenschneider, itsTylerIRL) and
 * GitHub Pages serves paths case-sensitively, so author URLs are always lowercase.
 */
export function authorSlug(author: string): string {
  return author.toLowerCase();
}

/** The app slug is its folder name: the collection id is "<folder>/manifest.yaml". */
export function appSlug(app: CollectionEntry<'apps'>): string {
  return app.id.split('/')[0];
}

/** All authors with their apps, most prolific first. */
export async function getAuthors(): Promise<Author[]> {
  const apps = await getCollection('apps');
  const authors = new Map<string, Author>();

  for (const app of apps) {
    const slug = authorSlug(app.data.author);
    let author = authors.get(slug);
    if (!author) {
      author = { slug, name: app.data.author, apps: [] };
      authors.set(slug, author);
    }
    author.apps.push(app);
  }

  for (const author of authors.values()) {
    author.apps.sort((a, b) => a.data.name.localeCompare(b.data.name));
  }

  return [...authors.values()].sort(
    (a, b) => b.apps.length - a.apps.length || a.name.localeCompare(b.name)
  );
}
