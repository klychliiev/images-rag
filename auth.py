import os
from typing import Optional, Tuple
import streamlit as st
from supabase import create_client, Client
from streamlit_cookies_controller import CookieController

from config import settings

_COOKIE_ACCESS = "sb_access_token"
_COOKIE_REFRESH = "sb_refresh_token"
_COOKIE_MAX_AGE_ACCESS = 60 * 60 * 24 * 7   # 7 days
_COOKIE_MAX_AGE_REFRESH = 60 * 60 * 24 * 30  # 30 days
_MAX_COOKIE_RETRIES = 3  # retries to wait for async cookie component to load


def _get_client() -> Client:
    if "supabase" not in st.session_state:
        url = settings.supabase_url
        key = settings.supabase_anon_key
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not set in environment.")
        st.session_state.supabase = create_client(url, key)
    return st.session_state.supabase


def _cookies() -> CookieController:
    if "cookie_ctrl" not in st.session_state:
        st.session_state.cookie_ctrl = CookieController()
    return st.session_state.cookie_ctrl


def _is_allowed_email(email: str) -> bool:
    return email.lower().endswith(settings.acceptable_email_domain)


def current_user():
    return st.session_state.get("user")


def _save_tokens(session) -> None:
    ctrl = _cookies()
    ctrl.set(_COOKIE_ACCESS, session.access_token, max_age=_COOKIE_MAX_AGE_ACCESS)
    ctrl.set(_COOKIE_REFRESH, session.refresh_token, max_age=_COOKIE_MAX_AGE_REFRESH)


def _clear_tokens() -> None:
    ctrl = _cookies()
    ctrl.remove(_COOKIE_ACCESS)
    ctrl.remove(_COOKIE_REFRESH)


def _restore_session_from_cookies() -> None:
    if st.session_state.get("user"):
        return
    ctrl = _cookies()
    access_token = ctrl.get(_COOKIE_ACCESS)
    refresh_token = ctrl.get(_COOKIE_REFRESH)
    if access_token and refresh_token:
        try:
            res = _get_client().auth.set_session(access_token, refresh_token)
            st.session_state.user = res.user
            st.session_state.session = res.session
            _save_tokens(res.session)
            st.session_state.pop("_cookie_retries", None)
        except Exception:
            _clear_tokens()
    else:
        # Cookie component loads asynchronously — rerun a few times before giving up
        retries = st.session_state.get("_cookie_retries", 0)
        if retries < _MAX_COOKIE_RETRIES:
            st.session_state["_cookie_retries"] = retries + 1
            st.rerun()


def sign_out():
    _get_client().auth.sign_out()
    _clear_tokens()
    for k in ("user", "session"):
        st.session_state.pop(k, None)


def sign_in(email: str, password: str) -> Tuple[Optional[dict], Optional[str]]:
    if not _is_allowed_email(email):
        return None, f"Only {settings.acceptable_email_domain} emails are allowed."
    try:
        res = _get_client().auth.sign_in_with_password({"email": email, "password": password})
        st.session_state.user = res.user
        st.session_state.session = res.session
        _save_tokens(res.session)
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


def auth_sidebar() -> bool:
    _restore_session_from_cookies()

    st.sidebar.header("🔐 Account")

    user = current_user()
    if user:
        st.sidebar.success(f"Signed in as: {user.email}")
        if st.sidebar.button("Sign out", use_container_width=True):
            sign_out()
            st.rerun()
        return True

    tab_login, tab_signup = st.sidebar.tabs(["Sign in", "Create account"])

    with tab_login:
        email = st.text_input("Work email", placeholder="you@artisio.co", key="login_email")
        password = st.text_input("Password", type="password", key="login_pw")

        col1, col2 = st.columns(2)

        login_clicked = False
        reset_clicked = False

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
                        {"redirect_to": "https://pinecone-manager-service.streamlit.app"}
                    )
                    st.success("📧 Reset email sent.")
                except Exception as e:
                    st.error(str(e))

    with tab_signup:
        with st.form("signup_form"):
            email = st.text_input("Work email", placeholder="you@artisio.co", key="signup_email")
            password = st.text_input("Password", type="password", key="signup_pw")
            submitted = st.form_submit_button("Create account", use_container_width=True)
            if submitted:
                err = sign_up(email, password)
                if err:
                    st.error(err)
                else:
                    st.success("Account created. Check your inbox to verify, then sign in.")

    return False
