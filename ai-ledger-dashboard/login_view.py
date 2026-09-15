import os
import streamlit as st
from api_client import ApiClient
from login_session import LoginSession, SupabasePasswordAuth, LoginError, LoginUnavailable


def require_login():
    try:
        provider=SupabasePasswordAuth(os.environ.get("SUPABASE_URL"),os.environ.get("SUPABASE_PUBLISHABLE_KEY"))
    except LoginError as exc:
        st.error(str(exc))
        st.stop()
    login=LoginSession(st.session_state,provider)
    try:
        token=login.access_token()
    except LoginUnavailable as exc:
        st.warning(str(exc))
        st.button("重试连接")
        logout_button(login)
        st.stop()
    except LoginError:
        token=None
        st.info("会话已过期，请重新登录。")
    if token:
        return token,login
    st.title("登录家庭账本")
    st.caption("使用管理员已开通的邮箱账号。忘记密码请联系管理员重置。")
    def submit():
        email=st.session_state.get("login_email","").strip()
        password=st.session_state.get("login_password","")
        st.session_state["login_password"]=""
        try:
            def authorize(access):
                client=ApiClient(auth_token=access)
                try:
                    client.request("GET","/api/v1/review",params={"limit":1})
                finally:
                    client.session.close()
            login.sign_in(email,password,authorize)
        except LoginError as exc:
            st.session_state["_login_error"]=str(exc)
    with st.form("login_form"):
        st.text_input("邮箱",key="login_email")
        st.text_input("密码",type="password",key="login_password")
        st.form_submit_button("登录",on_click=submit)
    if st.session_state.get("_login_error"):
        st.error(st.session_state["_login_error"])
    st.stop()


def logout_button(login):
    def logout():
        ok=login.sign_out()
        if not ok:
            st.session_state["_login_error"]="本次页面会话已清除；远端退出暂未完成。"
    st.button("退出登录",on_click=logout)
