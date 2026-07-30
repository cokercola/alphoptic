/**
 * Shared site header for Alphoptic. Injects the topbar + explainer strip
 * into any element with id="site-header". Edit the HTML in HEADER_HTML
 * below and every page picks up the change automatically.
 *
 * Usage in each page's <body>, right after the opening <body> tag:
 *   <div id="site-header"></div>
 *   <script src="/assets/header.js"></script>
 *
 * Remove any old inline <header class="topbar">...</header> and
 * <div class="explainer-strip">...</div> markup from the page — this
 * script replaces both.
 */
const HEADER_HTML = `
<header class="topbar">
  <div class="brand">
    <img src="/assets/logo.png" alt="Alphoptic" class="logo-mark">
    <span><span class="brand-alph">ALPH</span><span class="brand-optic">OPTIC</span></span>
  </div>
  <div id="donate-widget"></div>
  <div class="updated" id="updated-at">Loading...</div>
</header>
<div class="explainer-strip">
  <p>Alphoptic tracks bills in Congress, estimates their industry and company impact, and flags legislative signals — AI-assisted estimates, not predictions.</p>
</div>
`;

function initHeader() {
  const mount = document.getElementById("site-header");
  if (!mount) return;
  mount.innerHTML = HEADER_HTML;

  // Render the donate widget now that #donate-widget exists in the DOM.
  if (typeof renderDonateWidget === "function") {
    renderDonateWidget("donate-widget");
  }

  // Kick off the "Updated" timestamp if site.js exposes a function for it.
  if (typeof setUpdatedTimestamp === "function") {
    setUpdatedTimestamp();
  } else {
    // Fallback: just show current load time if no dedicated function exists.
    const updatedEl = document.getElementById("updated-at");
    if (updatedEl) {
      updatedEl.textContent = "Updated " + new Date().toLocaleString();
    }
  }
}

document.addEventListener("DOMContentLoaded", initHeader);
