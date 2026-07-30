/**
 * Donate button + popover for the topbar. Renders into any element with
 * id="donate-widget". Edit DONATION_LINKS below with your actual Stripe
 * Payment Link URLs once created (Dashboard -> Payment Links).
 */
const DONATION_LINKS = {
  5: "https://buy.stripe.com/6oU4gBce20oqgGkdPQebu00",
  10: "https://buy.stripe.com/aFacN73Hw3AC3Ty6noebu01",
  25: "https://buy.stripe.com/bJe7sNguigno4XCfXYebu02",
  custom: "https://buy.stripe.com/4gM9AV3Hwc78fCgbHIebu03",
};
function renderDonateWidget(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const isReady = (url) => url && !url.includes("REPLACE_WITH");
  const amountButtons = [5, 10, 25]
    .filter((amt) => isReady(DONATION_LINKS[amt]))
    .map((amt) => `<a class="donate-amount-btn" href="${DONATION_LINKS[amt]}" target="_blank" rel="noopener">$${amt}</a>`)
    .join("");
  const customButton = isReady(DONATION_LINKS.custom)
    ? `<a class="donate-custom-btn" href="${DONATION_LINKS.custom}" target="_blank" rel="noopener">Custom amount</a>`
    : "";
  el.innerHTML = `
    <div class="donate-wrap">
      <button class="donate-btn" id="donate-toggle" type="button">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 8h13a3 3 0 0 1 3 3v1a4 4 0 0 1 -4 4h-8a4 4 0 0 1 -4 -4v-4z"></path>
          <path d="M16 8.5h1.5a2.5 2.5 0 0 1 0 5h-1.5"></path>
          <path d="M8 1v2"></path>
          <path d="M11 1v2"></path>
        </svg>
        <span class="donate-label-full">Buy me a coffee</span><span class="donate-label-short">Coffee</span>
      </button>
      <div class="donate-popover" id="donate-popover">
        <div class="donate-popover-label">Support Alphoptic</div>
        <div class="donate-popover-text">Helps cover API &amp; data costs to keep Alphoptic free and independent.</div>
        <div class="donate-amounts">
          ${amountButtons}
        </div>
        ${customButton}
      </div>
    </div>
  `;
  const toggle = document.getElementById("donate-toggle");
  const popover = document.getElementById("donate-popover");
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.classList.toggle("open");
  });
  document.addEventListener("click", () => {
    popover.classList.remove("open");
  });
}
