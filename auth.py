import os
from typing import Optional, Tuple
import streamlit as st
from supabase import create_client, Client

from config import settings

def _get_client() -> Client:
    """Singleton-ish Supabase client kept in session_state."""
    if "supabase" not in st.session_state:
        url = settings.supabase_url
        key = settings.supabase_anon_key
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY not set in environment.")
        st.session_state.supabase = create_client(url, key)
    return st.session_state.supabase

def _is_allowed_email(email: str) -> bool:
    return email.lower().endswith(settings.acceptable_email_domain)

def current_user():
    """Return the cached user (if any)."""
    return st.session_state.get("user")

def sign_out():
    _get_client().auth.sign_out()
    for k in ("user", "session"):
        st.session_state.pop(k, None)

def sign_in(email: str, password: str) -> Tuple[Optional[dict], Optional[str]]:
    """Email/password sign in. Returns (user_dict, error_msg)."""
    if not _is_allowed_email(email):
        return None, f"Only {settings.acceptable_email_domain} emails are allowed."
    try:
        res = _get_client().auth.sign_in_with_password({"email": email, "password": password})
        st.session_state.user = res.user
        st.session_state.session = res.session
        return res.user, None
    except Exception as e:
        return None, str(e)

def sign_up(email: str, password: str) -> Optional[str]:
    """Create a new account (Supabase will send verification email if configured)."""
    if not _is_allowed_email(email):
        return f"Only {settings.acceptable_email_domain} emails are allowed."
    try:
        _get_client().auth.sign_up({"email": email, "password": password})
        return None
    except Exception as e:
        return str(e)


def auth_sidebar() -> bool:
    """
    Renders the account box in the sidebar.
    Returns True if the user is authenticated.
    """
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
        reset_error = None

        with col1:
            if st.button("Sign in", use_container_width=True):
                login_clicked = True

        with col2:
            if st.button("Forgot password?", use_container_width=True):
                reset_clicked = True

        # ---- HANDLE ACTIONS OUTSIDE COLUMNS ----

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
