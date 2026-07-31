import json
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from urllib.parse import quote, unquote

import streamlit as st
import streamlit.components.v1 as components
from streamlit_cookies_controller import CookieController
from supabase import create_client, Client

from config import settings

# ── Session config ────────────────────────────────────────────────────────────
SESSION_TIMEOUT_MINUTES: int = settings.session_timeout_minutes

# ── Cookie keys ───────────────────────────────────────────────────────────────
_COOKIE_ACCESS  = "sb_access_token"
_COOKIE_REFRESH = "sb_refresh_token"
_COOKIE_EXPIRY  = "sb_session_expiry"   # ISO-8601 UTC — rolling window end
_COOKIE_MAX_AGE = 60 * 60 * 24 * 7     # browser keeps cookies for 7 days

# Extending the rolling window rewrites a cookie (a component render); once a
# minute is plenty — every rerun would render one per user interaction.
_WINDOW_EXTEND_INTERVAL_S = 60


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client() -> Client:
    if "supabase" not in st.session_state:
        url = settings.supabase_url
        key = settings.supabase_anon_key
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not set.")
        st.session_state.supabase = create_client(url, key)
    return st.session_state.supabase


def _write_cookies(values: dict[str, Optional[str]]) -> None:
    """Write (str value) or delete (None) cookies on the app origin.

    Rendered as an invisible component whose JS runs in the frontend. The run
    that renders it MUST complete normally: st.rerun() in the same run tears
    the iframe down before its JS executes and the write is silently lost
    (verified empirically — a flush sleep does NOT prevent it). Callers that
    need to rerun set a _pending_cookie_* flag and let the next run write.
    """
    parts = []
    for name, value in values.items():
        if value is None:
            cookie = f"{name}=; Max-Age=0; Path=/; SameSite=Lax"
        else:
            cookie = f"{name}={quote(value, safe='')}; Max-Age={_COOKIE_MAX_AGE}; Path=/; SameSite=Lax"
        parts.append(f"window.parent.document.cookie = {json.dumps(cookie)};")
    components.html(f"<script>{''.join(parts)}</script>", height=0)


def _component_cookies() -> dict:
    """Browser cookies read in the frontend and delivered as a component value.

    Streamlit Cloud's proxy strips Cookie headers before they reach the app,
    so st.context.cookies is always empty there — a frontend read is the only
    one that works. The value needs one roundtrip: the first run returns {}
    and Streamlit auto-reruns when the real dict arrives.

    Must be called AT MOST ONCE per script run (the library writes to a
    widget-backed session_state key on repeat calls, which raises) — the
    result is cached in session_state for any later reader in the same run.
    """
    # Before the first render there may be a delivery still in flight — lets
    # the UI say "checking session…" instead of flashing the login form.
    st.session_state["_jar_pending"] = "_cookie_reader" not in st.session_state
    try:
        cookies = CookieController(key="_cookie_reader").getAll()
        jar = cookies if isinstance(cookies, dict) else {}
    except Exception:
        jar = {}
    st.session_state["_cookie_jar_cache"] = jar
    return jar


def session_restore_pending() -> bool:
    """True while the frontend cookie read may still be in flight."""
    return bool(st.session_state.get("_jar_pending"))


def _read_cookie(name: str, jar: Optional[dict] = None) -> Optional[str]:
    # Request headers first: synchronous and authoritative where the platform
    # passes them through (e.g. local runs). Falls back to the frontend jar.
    val = st.context.cookies.get(name)
    if not (isinstance(val, str) and val) and jar is not None:
        val = jar.get(name)
    if not isinstance(val, str) or not val:
        return None
    # Our writer percent-encodes values; unquote is a no-op if already plain.
    return unquote(val)


def _is_allowed_email(email: str) -> bool:
    return email.lower().endswith(settings.acceptable_email_domain)


def _new_expiry() -> str:
    """ISO timestamp SESSION_TIMEOUT_MINUTES from now (rolling window)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=SESSION_TIMEOUT_MINUTES)).isoformat()


def _save_tokens(session) -> None:
    """Persist tokens and extend the rolling session window."""
    _write_cookies({
        _COOKIE_ACCESS:  session.access_token,
        _COOKIE_REFRESH: session.refresh_token,
        _COOKIE_EXPIRY:  _new_expiry(),
    })
    # The save included a fresh expiry — no need for another extension write.
    st.session_state["_window_extended_at"] = time.monotonic()


def _clear_tokens() -> None:
    _write_cookies({_COOKIE_ACCESS: None, _COOKIE_REFRESH: None, _COOKIE_EXPIRY: None})
    # st.context.cookies keeps showing the deleted cookies until the next page
    # load — this flag stops the restore path from resurrecting the session.
    st.session_state["_cookies_cleared"] = True


def _extend_window() -> None:
    """Push the rolling-window cookie forward, at most once a minute."""
    now = time.monotonic()
    if now - st.session_state.get("_window_extended_at", 0.0) < _WINDOW_EXTEND_INTERVAL_S:
        return
    st.session_state["_window_extended_at"] = now
    _write_cookies({_COOKIE_EXPIRY: _new_expiry()})


def _flush_pending_cookie_ops() -> None:
    """Perform cookie writes deferred by sign_in/sign_out.

    Those handlers st.rerun() to refresh the UI, and a write rendered in the
    same run as a rerun is lost — so they queue the operation and this runs it
    at the top of the next (normal, run-to-completion) render.
    """
    pending_session = st.session_state.pop("_pending_cookie_save", None)
    if pending_session is not None:
        _save_tokens(pending_session)
    if st.session_state.pop("_pending_cookie_clear", False):
        _clear_tokens()


def _restore_session_from_cookies() -> None:
    """
    Called on every app load. Restores the Supabase session from cookies if:
      • tokens are present
      • the rolling session window has not expired
    Uses the refresh-token flow automatically when the access token is short-lived.
    Sets st.session_state['_session_expired'] = True when the window closes.
    """
    _flush_pending_cookie_ops()

    if st.session_state.get("user"):
        # Already authenticated — extend rolling window and return
        _extend_window()
        return

    if st.session_state.get("_cookies_cleared"):
        return  # signed out / cleared this session — headers are stale until reload

    jar = _component_cookies()
    access_token  = _read_cookie(_COOKIE_ACCESS, jar)
    refresh_token = _read_cookie(_COOKIE_REFRESH, jar)
    expiry_str    = _read_cookie(_COOKIE_EXPIRY, jar)

    if not (access_token and refresh_token):
        return  # genuinely signed out — no cookies in the request

    # ── Enforce session window ────────────────────────────────────────────────
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if datetime.now(timezone.utc) > expiry:
                _clear_tokens()
                st.session_state["_session_expired"] = True
                return
        except ValueError:
            pass  # malformed timestamp — let Supabase decide validity

    # ── Restore via Supabase (auto-refreshes access token if needed) ──────────
    try:
        res = _get_client().auth.set_session(access_token, refresh_token)
        st.session_state.user    = res.user
        st.session_state.session = res.session
        st.session_state.pop("_session_expired", None)
        if res.session.refresh_token != refresh_token:
            _save_tokens(res.session)   # token rotated — persist the new pair
        else:
            _extend_window()
    except Exception:
        _clear_tokens()


# ── Public API ────────────────────────────────────────────────────────────────

def current_user():
    return st.session_state.get("user")


def sign_in(email: str, password: str) -> Tuple[Optional[dict], Optional[str]]:
    if not _is_allowed_email(email):
        return None, f"Only {settings.acceptable_email_domain} emails are allowed."
    try:
        res = _get_client().auth.sign_in_with_password({"email": email, "password": password})
        st.session_state.user    = res.user
        st.session_state.session = res.session
        # Deferred: the UI reruns right after sign-in, which would kill a cookie
        # write rendered now. The next run persists the tokens.
        st.session_state["_pending_cookie_save"] = res.session
        st.session_state.pop("_session_expired", None)
        st.session_state.pop("_cookies_cleared", None)
        return res.user, None
    except Exception as e:
        return None, str(e)


def sign_up(email: str, password: str) -> Optional[str]:
    if not _is_allowed_email(email):
        return f"Only {settings.acceptable_email_domain} emails are allowed."
    try:
        _get_client().auth.sign_up({"email": email, "password": password})
        return None
    except Exception as e:
        return str(e)


def sign_out() -> None:
    try:
        _get_client().auth.sign_out()
    except Exception:
        pass
    # Deferred for the same rerun reason as sign_in.
    st.session_state["_pending_cookie_clear"] = True
    for k in ("user", "session", "_session_expired", "_window_extended_at", "_pending_cookie_save"):
        st.session_state.pop(k, None)


def auth_sidebar() -> bool:
    """Render the auth sidebar. Returns True if the user is authenticated."""
    _restore_session_from_cookies()

    st.sidebar.header("🔐 Account")

    if st.session_state.get("_session_expired"):
        st.sidebar.warning(
            f"Your session expired after {SESSION_TIMEOUT_MINUTES} minutes of inactivity. "
            "Please sign in again."
        )
        st.session_state.pop("_session_expired", None)

    user = current_user()
    if user:
        st.sidebar.success(f"Signed in as: {user.email}")
        st.sidebar.caption(f"Session renews on activity · {SESSION_TIMEOUT_MINUTES} min window")
        if st.sidebar.button("Sign out", use_container_width=True):
            sign_out()
            st.rerun()
        return True

    tab_login, tab_signup = st.sidebar.tabs(["Sign in", "Create account"])

    with tab_login:
        email    = st.text_input("Work email", placeholder="you@artisio.co", key="login_email")
        password = st.text_input("Password", type="password", key="login_pw")

        col1, col2 = st.columns(2)
        login_clicked = reset_clicked = False

        with col1:
            if st.button("Sign in", use_container_width=True):
                login_clicked = True
        with col2:
            if st.button("Forgot password?", use_container_width=True):
                reset_clicked = True

        if login_clicked:
            _, err = sign_in(email, password)
            if err:
                st.error(err)
            else:
                st.success("Signed in!")
                st.rerun()

        if reset_clicked:
            if not email:
                st.warning("Enter your email first.")
            else:
                try:
                    _get_client().auth.reset_password_email(
                        email,
                        {"redirect_to": "https://pinecone-manager-service.streamlit.app"},
                    )
                    st.success("📧 Reset email sent.")
                except Exception as e:
                    st.error(str(e))

    with tab_signup:
        with st.form("signup_form"):
            email    = st.text_input("Work email", placeholder="you@artisio.co", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_pw")
            submitted = st.form_submit_button("Create account", use_container_width=True)
            if submitted:
                err = sign_up(email, password)
                if err:
                    st.error(err)
                else:
                    st.success("Account created. Check your inbox to verify, then sign in.")

    return False
