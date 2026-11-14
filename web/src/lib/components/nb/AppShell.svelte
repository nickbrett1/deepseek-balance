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
    children,
    mark,
    actions,
    back,
    footer,
  } = $props();
</script>

<div class="shell">
  <header class="topbar">
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

  <footer class="footer">
    <div class="footer-inner">
      {#if footer}
        {@render footer()}
      {:else}
        <span class="micro">house design system · @nb</span>
      {/if}
    </div>
  </footer>
</div>

<style>
  .shell {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--ink);
  }

  .topbar {
    position: sticky;
    /* Not 0: on an iPhone home screen the status bar is drawn over the page, and
       a bar pinned at 0 slides under the clock. */
    top: var(--safe-top, 0px);
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
</style>