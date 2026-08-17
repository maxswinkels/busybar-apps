const ALL_TAGS = '__all__';
const DEFAULT_SORT = 'name';
const SORTS = ['name', 'newest', 'favorites'];
const URL_DEBOUNCE_MS = 300;

export function initGallery() {
  const grid = document.querySelector('.apps-grid');
  const searchInput = document.querySelector('#app-search');
  if (!grid || !searchInput) return;

  const sortSelect = document.querySelector('#app-sort');
  const filterButtons = [...document.querySelectorAll('.filter-button')];
  const resultCount = document.querySelector('#result-count');
  const emptyState = document.querySelector('#empty-state');
  const clearButton = document.querySelector('#clear-filters');

  const cards = [...grid.querySelectorAll('.app-card')];
  if (!cards.length) return;

  // Server-rendered order is alphabetical by name: the "name" sort and the
  // tiebreaker for every other sort.
  const nameOrder = new Map(cards.map((card, index) => [card, index]));

  const state = { q: '', tag: ALL_TAGS, sort: DEFAULT_SORT };

  function readUrl() {
    const params = new URLSearchParams(location.search);
    state.q = params.get('q') ?? '';
    state.tag = params.get('tag') || ALL_TAGS;
    const sort = params.get('sort');
    state.sort = SORTS.includes(sort) ? sort : DEFAULT_SORT;
  }

  function writeUrl(push) {
    const params = new URLSearchParams();
    if (state.q.trim()) params.set('q', state.q.trim());
    if (state.tag !== ALL_TAGS) params.set('tag', state.tag);
    if (state.sort !== DEFAULT_SORT) params.set('sort', state.sort);

    const query = params.toString();
    const url = query ? `${location.pathname}?${query}` : location.pathname;
    history[push ? 'pushState' : 'replaceState'](null, '', url);
  }

  let urlTimer;
  function writeUrlDebounced() {
    clearTimeout(urlTimer);
    urlTimer = setTimeout(() => writeUrl(false), URL_DEBOUNCE_MS);
  }

  function syncControls() {
    if (searchInput.value !== state.q) searchInput.value = state.q;
    if (sortSelect && sortSelect.value !== state.sort) sortSelect.value = state.sort;
    filterButtons.forEach(button => {
      button.classList.toggle(
        'filter-button-active',
        button.getAttribute('data-tag') === state.tag
      );
    });
  }

  // The favorites module fills these in once /counts resolves.
  function favCount(card) {
    return Number(card.querySelector('.fav-count')?.textContent) || 0;
  }

  // Every sort falls back to the alphabetical server order for ties, which also
  // covers apps with no add date (no git history at build time).
  function compare(a, b) {
    const byName = nameOrder.get(a) - nameOrder.get(b);
    if (state.sort === 'favorites') return favCount(b) - favCount(a) || byName;
    if (state.sort === 'newest') {
      // ISO 8601 timestamps sort correctly as plain strings.
      return (b.dataset.added ?? '').localeCompare(a.dataset.added ?? '') || byName;
    }
    return byName;
  }

  function render() {
    const tokens = state.q.toLowerCase().split(/\s+/).filter(Boolean);
    let visible = 0;

    for (const card of cards) {
      const haystack = card.dataset.search ?? '';
      const cardTags = (card.dataset.tags ?? '').split(' ');
      const show =
        (state.tag === ALL_TAGS || cardTags.includes(state.tag)) &&
        tokens.every(token => haystack.includes(token));

      card.hidden = !show;
      if (show) visible++;
    }

    const ordered = [...cards].sort(compare);
    const current = [...grid.children];
    if (ordered.some((card, index) => current[index] !== card)) {
      grid.append(...ordered);
    }

    if (resultCount) {
      resultCount.textContent =
        visible === cards.length
          ? `${cards.length} apps`
          : `${visible} of ${cards.length} apps`;
    }
    if (emptyState) emptyState.hidden = visible > 0;
  }

  searchInput.addEventListener('input', () => {
    state.q = searchInput.value;
    render();
    writeUrlDebounced();
  });

  filterButtons.forEach(button => {
    button.addEventListener('click', () => {
      state.tag = button.getAttribute('data-tag') ?? ALL_TAGS;
      syncControls();
      render();
      writeUrl(true);
    });
  });

  sortSelect?.addEventListener('change', () => {
    state.sort = sortSelect.value;
    render();
    writeUrl(true);
  });

  clearButton?.addEventListener('click', () => {
    state.q = '';
    state.tag = ALL_TAGS;
    syncControls();
    render();
    writeUrl(true);
    searchInput.focus();
  });

  window.addEventListener('popstate', () => {
    readUrl();
    syncControls();
    render();
  });

  // Favorite counts arrive after the page has rendered; re-sort when they do.
  document.addEventListener('busybar:counts', () => {
    if (state.sort === 'favorites') render();
  });

  document.addEventListener('keydown', event => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    const target = event.target;
    const typing =
      target instanceof HTMLElement &&
      (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable);

    if (event.key === '/' && !typing) {
      event.preventDefault();
      searchInput.focus();
      searchInput.select();
      return;
    }

    if (event.key === 'Escape' && target === searchInput) {
      if (state.q) {
        state.q = '';
        searchInput.value = '';
        render();
        writeUrlDebounced();
      } else {
        searchInput.blur();
      }
    }
  });

  readUrl();
  syncControls();
  render();
}
