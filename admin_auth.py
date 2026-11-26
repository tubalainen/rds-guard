"""Password login for the admin pages, shared by the satellite and the aggregator.

The password comes from ADMIN_PASSWORD in .env. Without one the admin pages
are switched off: the protected endpoints answer 403 and nobody can log in.

A login gives an HttpOnly, SameSite=Strict session cookie. Sessions live in
memory only, so a restart or a new password logs everyone out. Repeated wrong
passwords from one address are slowed down with a short lockout.
"""

import asyncio
import hashlib
import hmac
import secrets
import time

from aiohttp import web

SESSION_SEC = 12 * 3600
MAX_FAILS = 5            # wrong passwords per address ...
FAIL_WINDOW_SEC = 900    # ... within this window before a lockout
FAIL_DELAY_SEC = 0.5     # every wrong password waits this long


def _error(status, code, detail=""):
    return web.json_response({"error": code, "detail": detail}, status=status)


def _fingerprint(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class AdminAuth:
    """Login endpoints plus a middleware that guards the admin API paths.

    get_password: callable returning the current ADMIN_PASSWORD (read on
        every request, so tests and config reloads see the new value).
    protected: path prefixes that need a logged-in admin, for every method.
    cookie_name: differs between the apps, since browsers share cookies
        between ports on the same host.
    """

    def __init__(self, get_password, protected, cookie_name):
        self._get_password = get_password
        self._protected = tuple(protected)
        self.cookie_name = cookie_name
        self._sessions = {}   # token -> (expires_at, password fingerprint)
        self._fails = {}      # remote address -> [failure times]

    # ------------------------------------------------------------ state

    def _password(self):
        return (self._get_password() or "").strip()

    def enabled(self):
        return bool(self._password())

    def logged_in(self, request):
        password = self._password()
        if not password:
            return False
        token = request.cookies.get(self.cookie_name)
        entry = self._sessions.get(token) if token else None
        if entry is None:
            return False
        expires, fp = entry
        if expires < time.time() or fp != _fingerprint(password):
            self._sessions.pop(token, None)
            return False
        return True

    def _locked(self, addr, now):
        recent = [t for t in self._fails.get(addr, []) if now - t < FAIL_WINDOW_SEC]
        if recent:
            self._fails[addr] = recent
        else:
            self._fails.pop(addr, None)
        return len(recent) >= MAX_FAILS

    def _prune(self, now):
        for token, (expires, _) in list(self._sessions.items()):
            if expires < now:
                del self._sessions[token]

    # ------------------------------------------------------------ middleware

    @web.middleware
    async def middleware(self, request, handler):
        if request.path.startswith(self._protected):
            if not self.enabled():
                return _error(403, "admin_disabled",
                              "sätt ADMIN_PASSWORD i .env för att använda admin")
            if not self.logged_in(request):
                return _error(401, "admin_required", "logga in som admin")
        return await handler(request)

    # ------------------------------------------------------------ handlers

    async def handle_session(self, request):
        return web.json_response({"enabled": self.enabled(),
                                  "logged_in": self.logged_in(request)})

    async def handle_login(self, request):
        password = self._password()
        if not password:
            return _error(403, "admin_disabled",
                          "sätt ADMIN_PASSWORD i .env för att använda admin")
        now = time.time()
        addr = request.remote or "?"
        if self._locked(addr, now):
            return _error(429, "too_many_attempts",
                          "för många felaktiga försök, vänta en stund")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        given = body.get("password") if isinstance(body, dict) else None
        if not isinstance(given, str) or not hmac.compare_digest(
                given.encode("utf-8"), password.encode("utf-8")):
            self._fails.setdefault(addr, []).append(now)
            await asyncio.sleep(FAIL_DELAY_SEC)
            return _error(401, "wrong_password", "fel lösenord")

        self._fails.pop(addr, None)
        self._prune(now)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (now + SESSION_SEC, _fingerprint(password))
        resp = web.json_response({"enabled": True, "logged_in": True})
        resp.set_cookie(self.cookie_name, token, max_age=SESSION_SEC, path="/",
                        httponly=True, samesite="Strict",
                        secure=_is_https(request))
        return resp

    async def handle_logout(self, request):
        token = request.cookies.get(self.cookie_name)
        if token:
            self._sessions.pop(token, None)
        resp = web.json_response({"enabled": self.enabled(), "logged_in": False})
        resp.del_cookie(self.cookie_name, path="/")
        return resp

    def add_routes(self, router, base):
        """base: the API prefix, e.g. "/api" or "/api/v1"."""
        router.add_get(f"{base}/admin/session", self.handle_session)
        router.add_post(f"{base}/admin/login", self.handle_login)
        router.add_post(f"{base}/admin/logout", self.handle_logout)


def _is_https(request):
    if request.secure:
        return True
    return request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
