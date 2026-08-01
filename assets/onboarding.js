/**
 * Dashboard onboarding card. Shows once for first-time visitors,
 * dismisses permanently via localStorage once closed.
 *
 * Usage: <div id="onboarding-card"></div>
 *        <script src="/assets/onboarding.js"></script>
 */
const ONBOARDING_DISMISSED_KEY = 'alphoptic_onboarding_dismissed';

const ONBOARDING_HTML = `
<div class="onboarding-card">
  <button class="onboarding-close" id="onboarding-close" type="button" aria-label="Dismiss">✕</button>
  <div class="onboarding-title">Welcome to Alphoptic</div>
  <div class="onboarding-bullet">
    <span class="onboarding-dot"></span>
    <span>Alphoptic tracks bills moving through Congress and estimates which industries and companies they're likely to affect.</span>
  </div>
  <div class="onboarding-bullet">
    <span class="onboarding-dot"></span>
    <span>Each bill gets an AI-assessed direction and impact score — click the <span class="info-icon-inline">i</span> icons throughout the site for a plain-language explanation of any number you see.</span>
  </div>
  <div class="onboarding-bullet">
    <span class="onboarding-dot"></span>
    <span>This is early-stage and actively growing — expect more bills, lawmaker coverage, and features soon.</span>
  </div>
  <div class="onboarding-link">
    <a href="/about.html">Learn more about Alphoptic →</a>
  </div>
</div>
`;

function initOnboardingCard() {
  const mount = document.getElementById('onboarding-card');
  if (!mount) return;

  let dismissed = false;
  try {
    dismissed = localStorage.getItem(ONBOARDING_DISMISSED_KEY) === '1';
  } catch (e) {
    // localStorage unavailable (privacy mode, etc.) - just show the
    // card every time in that case rather than breaking.
  }
  if (dismissed) return;

  mount.innerHTML = ONBOARDING_HTML;

  document.getElementById('onboarding-close').addEventListener('click', () => {
    mount.innerHTML = '';
    try {
      localStorage.setItem(ONBOARDING_DISMISSED_KEY, '1');
    } catch (e) {
      // If localStorage isn't available, the card just won't
      // remember the dismissal across visits - not a big deal.
    }
  });
}

document.addEventListener('DOMContentLoaded', initOnboardingCard);
