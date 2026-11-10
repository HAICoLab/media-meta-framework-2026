"""
브라우저 컨텍스트 싱글톤 모듈
- Playwright 브라우저 인스턴스를 전역으로 관리
- 서버 수명과 동일하게 유지, 닫히면 자동 재시작
"""

import logging

_pw = None
_browser = None
_context = None

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


async def init_browser():
    """서버 시작 시 브라우저 초기화."""
    global _pw, _browser, _context
    from playwright.async_api import async_playwright
    _pw = await async_playwright().start()
    try:
        _browser = await _pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    except Exception as e:
        # PLAYWRIGHT_BROWSERS_PATH 가 임시 캐시 등 브라우저가 없는 경로를 가리키면 기본 설치 경로(~/.cache/ms-playwright)로 한 번 더 시도한다
        import os
        default = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")
        if "Executable doesn't exist" not in str(e) or not os.path.isdir(default) \
                or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == default:
            raise
        logging.warning(f"브라우저 실행 파일을 찾지 못해 기본 경로로 재시도: {default}")
        await _pw.stop()
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = default
        _pw = await async_playwright().start()
        _browser = await _pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    _context = await _browser.new_context(user_agent=_USER_AGENT, locale="ko-KR")
    logging.info("브라우저 초기화 완료")


async def close_browser():
    """서버 종료 시 브라우저 해제."""
    global _pw, _browser, _context
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
    if _pw:
        try:
            await _pw.stop()
        except Exception:
            pass
    _context = None
    _browser = None
    _pw = None
    logging.info("브라우저 종료 완료")


async def get_context():
    """브라우저 컨텍스트를 반환. 닫혀있으면 자동 재시작."""
    global _pw, _browser, _context

    if _context:
        try:
            test_page = await _context.new_page()
            await test_page.close()
            return _context
        except Exception:
            pass

    logging.warning("브라우저 컨텍스트 닫힘 감지 → 재시작")
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass

    if not _pw:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()

    _browser = await _pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
    _context = await _browser.new_context(user_agent=_USER_AGENT, locale="ko-KR")
    logging.info("브라우저 재시작 완료")
    return _context
