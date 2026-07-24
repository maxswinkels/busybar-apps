import { defineCollection } from 'astro:content';
import { glob } from 'astro/loaders';
import { z } from 'astro/zod';

const apps = defineCollection({
  loader: glob({ pattern: '*/manifest.yaml', base: './apps' }),
  schema: ({ image }) => z.object({
    name: z.string().min(1),
    author: z.string().min(1),
    description: z.string().min(1).max(200),
    tags: z.array(z.string()).default([]),
    preview: image(),
    repo: z.string().url().optional(),
  }),
});

export const collections = { apps };
