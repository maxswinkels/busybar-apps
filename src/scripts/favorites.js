// Override locally via .env: PUBLIC_FAVORITES_API=http://localhost:8787
const API = import.meta.env.PUBLIC_FAVORITES_API ?? 'https://busybar-favorites.jolly-sky-1dbb.workers.dev';

const LS_KEY = 'busybar-favorites';

function loadSet() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function saveSet(set) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify([...set]));
  } catch {}
}

export function initFavorites() {
  const buttons = document.querySelectorAll('button[data-favorite-slug]');
  if (!buttons.length) return;

  const favorites = loadSet();

  buttons.forEach(button => {
    const slug = button.dataset.favoriteSlug;
    button.setAttribute('aria-pressed', favorites.has(slug) ? 'true' : 'false');
  });

  fetch(API + '/counts')
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data?.counts) return;
      buttons.forEach(button => {
        const slug = button.dataset.favoriteSlug;
        const count = data.counts[slug] ?? 0;
        const span = button.querySelector('.fav-count');
        if (span) {
          span.textContent = count;
          span.hidden = count === 0;
        }
      });
      // Lets the gallery re-sort once real counts are on the page.
      document.dispatchEvent(new CustomEvent('busybar:counts'));
    })
    .catch(() => {});

  buttons.forEach(button => {
    button.addEventListener('click', () => {
      const slug = button.dataset.favoriteSlug;
      const favorites = loadSet();
      const adding = !favorites.has(slug);

      if (adding) {
        favorites.add(slug);
      } else {
        favorites.delete(slug);
      }
      saveSet(favorites);
      button.setAttribute('aria-pressed', adding ? 'true' : 'false');

      const span = button.querySelector('.fav-count');
      const prev = span ? Number(span.textContent) || 0 : 0;
      if (span) {
        const next = Math.max(0, adding ? prev + 1 : prev - 1);
        span.textContent = next;
        span.hidden = next === 0;
      }

      const revert = () => {
        if (span) {
          span.textContent = prev;
          span.hidden = prev === 0;
        }
      };

      fetch(API + '/favorite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug, action: adding ? 'add' : 'remove' }),
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && typeof data.count === 'number') {
            if (span) {
              span.textContent = data.count;
              span.hidden = data.count === 0;
            }
          } else {
            // Server rejected the toggle (e.g. rate limited): undo the optimistic count.
            revert();
          }
        })
        .catch(revert);
    });
  });
}
