/**
 * Donate button + popover for the topbar. Renders into any element with
 * id="donate-widget". Edit DONATION_LINKS below with your actual Stripe
 * Payment Link URLs once created (Dashboard -> Payment Links).
 */

const DONATION_LINKS = {
  5: "https://buy.stripe.com/REPLACE_WITH_5_DOLLAR_LINK",
  10: "https://buy.stripe.com/REPLACE_WITH_10_DOLLAR_LINK",
  25: "https://buy.stripe.com/REPLACE_WITH_25_DOLLAR_LINK",
  custom: "https://buy.stripe.com/REPLACE_WITH_CUSTOM_AMOUNT_LINK",
};

function renderDonateWidget(containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;

  el.innerHTML = `
    <div class="donate-wrap">
      <button class="donate-btn" id="donate-toggle" type="button">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 8h13a3 3 0 0 1 3 3v1a4 4 0 0 1 -4 4h-8a4 4 0 0 1 -4 -4v-4z"></path>
          <path d="M16 8.5h1.5a2.5 2.5 0 0 1 0 5h-1.5"></path>
          <path d="M8 1v2"></path>
          <path d="M11 1v2"></path>
        </svg>
        Buy me a coffee
      </button>
      <div class="donate-popover" id="donate-popover">
        <div class="donate-popover-label">Support Alphoptic</div>
        <div class="donate-amounts">
          <a class="donate-amount-btn" href="${DONATION_LINKS[5]}" target="_blank" rel="noopener">$5</a>
          <a class="donate-amount-btn" href="${DONATION_LINKS[10]}" target="_blank" rel="noopener">$10</a>
          <a class="donate-amount-btn" href="${DONATION_LINKS[25]}" target="_blank" rel="noopener">$25</a>
        </div>
        <a class="donate-custom-btn" href="${DONATION_LINKS.custom}" target="_blank" rel="noopener">Custom amount</a>
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
