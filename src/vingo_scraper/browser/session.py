"""Playwright context factory with persistent per-account profiles."""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import structlog
from playwright.async_api import BrowserContext, Page, async_playwright

from vingo_scraper.browser import stealth
from vingo_scraper.config import LOGIN_WALL_MARKERS, settings

log = structlog.get_logger(__name__)


class LoginRequiredError(RuntimeError):
    """Marketplace bounced us to login/checkpoint.

    Raised loudly rather than returning an empty list, because a silent empty
    result looks identical to "no listings matched" and would let a dead
    scraper appear healthy for weeks.
    """


class RateLimitExceeded(RuntimeError):
    """The account hit its daily page budget. Stop, don't push through."""


@dataclass
class ProxyConfig:
    """Sticky residential proxy, pinned per account.

    One account keeps one exit IP for its lifetime: rotating IPs underneath a
    single logged-in account is itself a strong ban signal.
    """

    server: str
    username: str | None = None
    password: str | None = None

    def to_playwright(self) -> dict[str, str]:
        cfg = {"server": self.server}
        if self.username:
            cfg["username"] = self.username
            cfg["password"] = self.password or ""
        return cfg


async def human_delay(min_s: float | None = None, max_s: float | None = None) -> None:
    """Randomized pause between actions.

    Uniform jitter, not a fixed sleep: a constant interval is a trivially
    detectable machine signature.
    """
    lo = settings.min_delay_s if min_s is None else min_s
    hi = settings.max_delay_s if max_s is None else max_s
    await asyncio.sleep(random.uniform(lo, hi))


def is_login_wall(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in LOGIN_WALL_MARKERS)


@asynccontextmanager
async def browser_session(
    account_id: str = "default",
    *,
    proxy: ProxyConfig | None = None,
    headless: bool | None = None,
):
    """Yield a hardened BrowserContext bound to one account's profile.

    Uses a persistent context so cookies and localStorage survive between runs.
    Re-authenticating on every run is both slow and a reliable way to trigger
    checkpoints.
    """
    profile_path: Path = settings.profile_dir / account_id
    profile_path.mkdir(parents=True, exist_ok=True)

    use_headless = settings.headless if headless is None else headless

    async with async_playwright() as pw:
        context: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=use_headless,
            args=stealth.LAUNCH_ARGS,
            user_agent=stealth.USER_AGENT,
            viewport=stealth.VIEWPORT,
            locale=settings.locale,
            timezone_id=settings.timezone,
            proxy=proxy.to_playwright() if proxy else None,
            ignore_default_args=["--enable-automation"],
        )
        await context.add_init_script(stealth.STEALTH_JS)
        context.set_default_navigation_timeout(settings.nav_timeout_ms)

        log.info("session.open", account=account_id, headless=use_headless,
                 proxy=bool(proxy))
        try:
            yield context
        finally:
            await context.close()
            log.info("session.close", account=account_id)


async def goto_checked(page: Page, url: str) -> None:
    """Navigate, then assert we did not land on a login/checkpoint wall."""
    await page.goto(url, wait_until="domcontentloaded")
    if is_login_wall(page.url):
        raise LoginRequiredError(
            f"redirected to {page.url} -- session is dead or challenged"
        )


async def is_authenticated(context: BrowserContext) -> bool:
    """Cheap liveness check on the stored session.

    Probes a *search* URL, not the Marketplace landing page. Logged-out
    visitors are served a landing page at /marketplace/ without a redirect,
    so checking that URL reports a live session when there is none -- and the
    failure then surfaces later, per-cell, looking like a scrape bug. Search
    is the path that actually gates, so it is the one worth testing.
    """
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.facebook.com/marketplace/mumbai/search?query=test",
            wait_until="domcontentloaded",
        )
        return not is_login_wall(page.url)
    except Exception:
        return False
    finally:
        await page.close()


async def interactive_login(account_id: str = "default") -> bool:
    """Open a headed browser so a human can log in once.

    Credentials are never stored by this tool -- the operator types them into a
    real browser window and we keep only the resulting session in the profile
    directory. Automated credential submission is what trips checkpoints.
    """
    async with browser_session(account_id, headless=False) as context:
        page = await context.new_page()
        await page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        print(f"\n  Log in as '{account_id}' in the browser window.")
        print("  Waiting up to 5 minutes for Marketplace to become reachable...\n")

        for _ in range(60):  # 60 * 5s = 5 minutes
            await asyncio.sleep(5)
            if not is_login_wall(page.url):
                if await is_authenticated(context):
                    log.info("login.success", account=account_id)
                    print(f"  Session saved to {settings.profile_dir / account_id}")
                    return True
        log.warning("login.timeout", account=account_id)
        return False
