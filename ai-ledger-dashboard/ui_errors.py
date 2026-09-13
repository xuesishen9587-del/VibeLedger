import streamlit as st
from api_client import AuthError, ConflictError, ApiError
from login_session import LoginError


def handle_api_error(error,context="请求失败"):
    if isinstance(error,(AuthError,LoginError)):
        st.error("登录会话暂不可用，请重试或重新登录。")
    elif isinstance(error,ConflictError):
        st.error("记录已被修改，请重新加载后核对并保存。")
    elif isinstance(error,ApiError):
        st.error(context+"："+error.message)
    else:
        st.error(context+"，请稍后重试。")
