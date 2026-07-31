/**
 * Shared site footer for Alphoptic. Injects into any element with
 * id="site-footer". Edit FOOTER_HTML below and every page picks up
 * the change automatically — same pattern as header.js.
 *
 * Usage in each page, right before </body>:
 *   <div id="site-footer"></div>
 *   <script src="/assets/footer.js"></script>
 */
const FOOTER_HTML = `
<footer class="site-footer">
  <div class="footer-brand">
    <img src="/assets/logo.png" alt="Alphoptic" class="footer-logo-mark">
    <span><span class="brand-alph">ALPH</span><span class="brand-optic">OPTIC</span></span>
  </div>
  <nav class="footer-links">
    <a href="/about.html">About</a>
    <a href="/privacy.html">Privacy Policy</a>
    <a href="/terms.html">Terms</a>
    <a href="/contact.html">Contact</a>
  </nav>
  <div class="footer-disclaimer">
    Not financial or legislative advice. Bill data is pulled from Congress.gov;
    industry and company impact estimates are AI-assisted, not predictions.
  </div>
  <div class="footer-copyright">
    &copy; ${new Date().getFullYear()} Alphoptic. All rights reserved.
  </div>
</footer>
`;

function initFooter() {
  const mount = document.getElementById("site-footer");
  if (!mount) return;
  mount.innerHTML = FOOTER_HTML;
}

document.addEventListener("DOMContentLoaded", initFooter);
