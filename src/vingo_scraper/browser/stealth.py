"""Fingerprint hardening for the Playwright context.

This closes the obvious automation tells (navigator.webdriver, empty plugin
array, missing chrome runtime). It is necessary but NOT sufficient -- it does
not defeat behavioural detection, and no amount of JS patching substitutes for
sane request rates and residential IPs. Treat this as hygiene, not a cloak.
"""

from __future__ import annotations

# Applied via add_init_script so it runs before any page script sees `window`.
STEALTH_JS = """
// navigator.webdriver is the single loudest automation tell.
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Headless Chrome ships an empty plugin array; real Chrome does not.
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(i => ({name: `Plugin ${i}`, filename: `p${i}.dll`})),
});

Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-US', 'en']});

// Headless reports 0 here.
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// window.chrome is absent in headless.
if (!window.chrome) { window.chrome = {runtime: {}}; }

// Headless denies notifications outright; real Chrome returns 'default'.
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) => (
  params.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(params)
);

// WebGL vendor/renderer strings leak SwiftShader in headless.
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';               // UNMASKED_VENDOR_WEBGL
  if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
  return getParameter.call(this, p);
};
"""

# A current, boring desktop Chrome UA. Must be kept roughly in step with the
# bundled Chromium major version or the mismatch is itself a signal.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1440, "height": 900}

# Flags that remove automation banners and reduce headless-specific tells.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]
