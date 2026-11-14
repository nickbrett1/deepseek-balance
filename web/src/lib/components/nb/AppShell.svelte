<script>
  /**
   * AppShell — the house application frame.
   *
   * Replaces the bespoke top bars in stratechery-search, deepseek-balance and
   * data-download. Sticky header with a backdrop blur, wordmark + micro subtitle,
   * back pill for non-root views, 68rem main, and a footer strip.
   *
   * Two slots, added in 1.4.0 because the three consumers all needed them and
   * all three had to bypass the block to get them:
   *
   *   `mark`    -- a logo or monogram, before the titles. Without it the only way
   *                to get a bespoke wordmark was to stop using the shell.
   *   `actions` -- everything on the right of the bar: primary navigation, a
   *                search box, an account button. `status` alone forced a second
   *                bar to sit under this one.
   *   `back`    -- replaces the back pill with your own control (1.5.0). The
   *                pill is a *link*, which is wrong for an app whose back is an
   *                action: `history.back()` returns to the results page you
   *                left, querystring and all, while a link pushes a new entry.
   *                An in-app router therefore has to hand its own button in.
   *   `titleTag` -- the wordmark's element, defaulting to an `h1` (1.6.0). The
   *                bar is the *page* title on a dashboard, but a reading app or
   *                a gallery already has an h1 in the content, and the wordmark
   *                is then the name of the product, not the page. Without this
   *                the shell could only be used by renaming that heading.
   *   `nav`     -- the app's top-level views, as `[{ label, href, current }]`
   *                (1.7.0). This is a *slot the block fills*, not a snippet:
   *                data-download put a pill-shaped link to its other view in
   *                `actions`, and on a phone a state pill and a destination
   *                look the same, so nobody could tell what views existed.
   *                Navigation is the bar's job and it has one shape -- a tab
   *                row with `aria-current="page"` -- so the block owns it and
   *                every consumer stops re-inventing it. `current` is the
   *                caller's truth (it knows its router); the block never
   *                guesses from the URL.
   *   `embedded` -- true when the shell is not a page: a Homepage dashboard
   *                card iframed into someone else's layout (1.11.0). A card is
   *                not a viewport, so the shell must not claim one -- and this
   *                is not cosmetic. `min-height: 100dvh` makes `<body>` at
   *                least as tall as the iframe, so a widget that reports its
   *                height to its parent measures the *viewport* instead of its
   *                content, and every poll can only ever call itself bigger.
   *                That is the exact feedback loop deepseek-balance fixed in
   *                2026-09 and re-created by adopting this block. Embedded also
   *                drops the default house footer (a card has no room for
   *                chrome and the parent owns the surroundings); an explicit
   *                `footer` snippet still renders, because then the consumer
   *                asked for it.
   *
   * The bar and the main column also honour the safe-area insets on a phone, so
   * an app added to a home screen does not put its nav under the clock.
   */
  let {
    title = "Application",
    subtitle = "",
    titleTag = "h1",
    backHref = "",
    backLabel = "← back",
    status = "",
    statusTone = "ok",
    nav = [],
    navLabel = "Sections",
    embedded = false,
    children,
    mark,
    actions,
    back,
    footer,
  } = $props();
</script>

<div class="shell" class:embedded>
  <header class="topbar" class:has-nav={nav?.length}>
    <div class="topbar-inner">
      <div class="brand">
        {#if mark}
          <span class="mark">{@render mark()}</span>
        {/if}
        {#if back}
          {@render back()}
        {:else if backHref}
          <a href={backHref} class="pill back">{backLabel}</a>
        {/if}
        <div class="titles">
          {#if subtitle}
            <span class="micro">{subtitle}</span>
          {/if}
          <svelte:element this={titleTag} class="wordmark">{title}</svelte:element>
        </div>
      </div>
      {#if nav?.length}
        <nav class="main-nav" aria-label={navLabel}>
          {#each nav as item}
            <a
              href={item.href}
              aria-current={item.current ? "page" : undefined}
              >{item.label}</a
            >
          {/each}
        </nav>
      {/if}
      {#if actions || status}
        <div class="bar-end">
          {#if actions}
            {@render actions()}
          {/if}
          {#if status}
            <span class="pill {statusTone}">{status}</span>
          {/if}
        </div>
      {/if}
    </div>
  </header>

  <main class="content">
    {@render children?.()}
  </main>

  {#if footer}
    <footer class="footer">
      <div class="footer-inner">
        {@render footer()}
      </div>
    </footer>
  {:else if !embedded}
    <footer class="footer">
      <div class="footer-inner">
        <span class="micro">house design system · @nb</span>
      </div>
    </footer>
  {/if}
</div>

<style>
  .shell {
    min-height: 100vh;
    /* The dynamic viewport height, where it exists: on iOS a `100vh` page is
       taller than the visible area while the toolbar is showing, so the footer
       sits below the fold and a never-scrolls page still scrolls. */
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--ink);
  }

  /* Embedded: a dashboard card in someone else's iframe, not a page. It must
     not claim a viewport -- `min-height: 100dvh` here would make <body> at
     least as tall as the iframe, so a widget that reports its own height to
     the parent (Homepage's `homepage-iframe-resize`) would measure the frame
     instead of its content and ask to grow on every poll. The shell is then
     exactly as tall as what it contains. */
  .shell.embedded {
    min-height: 0;
  }

  .topbar {
    position: sticky;
    /* `top: 0`, and the notch inset as *padding* -- not as the sticky offset.
       A sticky element with `top: 47px` is painted 47px lower while its flow
       box stays where it was, so it covers the first 47px of the page. On a
       notched iPhone that is the page's own title, and it is not a little
       clipped: it is entirely behind the bar (measured: bar 47-102, lede
       79-101, elementFromPoint at the lede returned .topbar-inner). Padding
       grows the bar instead, so the flow moves with it, and the bar's own
       background covers the status-bar band. */
    top: 0;
    padding-top: var(--safe-top, 0px);
    z-index: 20;
    /* Derived from the ground token, not a literal: the bar is the page
       colour at 88%, which is what makes the blur read as glass. */
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--line);
  }

  .topbar-inner {
    max-width: 68rem;
    margin: 0 auto;
    padding: 12px calc(16px + var(--safe-right, 0px)) 12px
      calc(16px + var(--safe-left, 0px));
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 12px;
    min-width: 0;
  }

  .mark {
    display: inline-flex;
    flex: none;
  }

  /* The bar's navigation: the app's top-level views, always both visible.
     A tab row, not pills -- a destination must not look like a state. */
  .main-nav {
    display: flex;
    align-items: center;
    gap: 2px;
    /* Hug the brand and let the bar's tail take the right-hand end. */
    margin-right: auto;
    min-width: 0;
    overflow-x: auto;
    /* A phone shows two or three views; a scrollbar under the tabs would eat
       the bar's height, and momentum scrolling still reaches the rest. */
    scrollbar-width: none;
  }

  .main-nav::-webkit-scrollbar {
    display: none;
  }

  .main-nav a {
    padding: 4px 10px;
    border-radius: var(--radius-sm);
    color: var(--ink-muted);
    font-size: 12.5px;
    line-height: 1.4;
    white-space: nowrap;
    /* 32px of touch target is the floor: the row's height is set by the
       wordmark anyway, so a shorter tab only shrinks what you can hit. */
    min-height: 30px;
    display: inline-flex;
    align-items: center;
  }

  .main-nav a:hover {
    background: var(--bg-hover);
    color: var(--ink);
    text-decoration: none;
  }

  .main-nav a[aria-current="page"] {
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 600;
  }

  .bar-end {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    justify-content: flex-end;
    /* The bar's tail takes the width it needs and the *titles* give way: a nav
       squeezed by a long wordmark wraps into two rows, which makes the sticky
       bar tall on exactly the screen where height is dearest. */
    flex: 0 0 auto;
  }

  .titles {
    display: flex;
    flex-direction: column;
    /* The product name is a wordmark, not a paragraph: a title that wraps makes
       the sticky bar two rows tall on a phone, where the height is the most
       expensive thing on the screen. It truncates instead. */
    min-width: 0;
  }

  .wordmark {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: var(--ink);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .back {
    text-decoration: none;
  }

  .content {
    flex: 1;
    width: 100%;
    max-width: 68rem;
    margin: 0 auto;
    padding: 24px calc(16px + var(--safe-right, 0px)) 24px
      calc(16px + var(--safe-left, 0px));
  }

  .footer {
    border-top: 1px solid var(--line);
    padding: 16px calc(16px + var(--safe-right, 0px))
      calc(16px + var(--safe-bottom, 0px)) calc(16px + var(--safe-left, 0px));
    background: var(--bg);
  }

  .footer-inner {
    max-width: 68rem;
    margin: 0 auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  @media (max-width: 520px) {
    /* With tabs in the bar, the micro subtitle is what to give up: it repeats
       what the highlighted tab already says, and on a 360px phone it is the
       thing that pushes the second view out of reach. The tabs stay. */
    .has-nav .titles .micro {
      display: none;
    }

    .topbar-inner {
      gap: 10px;
    }

    .main-nav a {
      padding: 4px 8px;
      font-size: 12px;
    }
  }
</style>
