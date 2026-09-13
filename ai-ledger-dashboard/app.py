"""Four-page Dashboard; financial access requires a session login."""
import streamlit as st
from api_client import ApiClient
from login_view import require_login, logout_button
from ui_errors import handle_api_error


class SessionApiClient(ApiClient):
    def __init__(self,login,token):
        super().__init__(auth_token=token)
        self.login=login

    def request(self,*args,**kwargs):
        from api_client import AuthError
        token=self.login.access_token()
        if not token:
            raise AuthError("请重新登录。",status_code=401)
        self.set_auth_token(token)
        return super().request(*args,**kwargs)


def main():
    st.set_page_config(page_title="家庭账本",page_icon="🏦",layout="wide")
    token,login=require_login()
    client=SessionApiClient(login,token)
    st.sidebar.title("家庭账本")
    menu=st.sidebar.radio("页面",["财富","支出","复核","设置"],key="main_page")
    try:
        if menu=="财富":
            from wealth_page import render
            from investment_view import render as investments
            render(client)
            investments(client)
            st.caption("需要导入账户账单？请进入支出页的账单导入。")
        elif menu=="支出":
            section=st.radio("支出功能",["记录与月度计划","账单导入"],horizontal=True,key="spending_section")
            if section=="账单导入":
                from statement_page import render
                render(client)
            else:
                from spending_page import render
                render(client)
        elif menu=="复核":
            from spending_page import render
            from spending_controller import SpendingActions
            from wealth_page import render_balance_drafts
            from statement_page import render as statements
            from investment_view import render as investments
            st.caption("草稿尚未入账；已保存记录的补充信息和投资变动复核不会再次记账。")
            render(client,review_only=True)
            st.subheader("待保存余额截图与账单")
            render_balance_drafts(client,SpendingActions(st.session_state.setdefault("_wealth_actions",{}),client))
            statements(client,review_only=True)
            investments(client,review_only=True)
        else:
            from settings_page import render
            from investment_view import render_settings
            from device_view import render as devices
            logout_button(login)
            render(client)
            render_settings(client)
            devices(client)
    except Exception as exc:
        handle_api_error(exc,"加载页面失败")
    finally:
        client.session.close()


main()
