import streamlit as st
from spending_controller import SpendingActions
from ui_errors import handle_api_error


def render(client):
    with st.expander("快捷指令设备"):
        st.caption("为自己的快捷指令创建设备凭证，或停用不再使用的设备。")
        actions=SpendingActions(st.session_state.setdefault("_device_actions",{}),client)
        for slot in list(actions.state):
            if st.button("重试设备停用",key="device_retry_"+slot):
                try:
                    actions.retry(slot)
                    st.rerun()
                except Exception as exc:
                    handle_api_error(exc,"停用设备失败")
        devices=client.request("GET","/api/v1/devices")["items"]
        for device in devices:
            st.write(device["device_name"],"已停用" if device["status"]=="revoked" else "可用")
            if device["status"]=="active" and st.button("停用此设备",key="device_revoke_"+device["device_id"]):
                try:
                    actions.execute(device["device_id"],"POST",f"/api/v1/devices/{device['device_id']}/revoke",{})
                    st.rerun()
                except Exception as exc:
                    handle_api_error(exc,"停用设备失败")
        with st.form("provision_device",clear_on_submit=True):
            name=st.text_input("新设备名称")
            if st.form_submit_button("创建设备凭证"):
                if not name.strip():
                    st.error("请填写设备名称。")
                else:
                    try:
                        result=client.request("POST","/api/v1/devices",json_data={"device_name":name.strip(),"platform":"ios_shortcuts"})
                        st.info("凭证仅显示一次，请立即保存到此设备的快捷指令中。")
                        st.code(result["token"],language=None)
                    except Exception:
                        st.warning("创建设备未得到确定结果。请先刷新设备列表核对，避免重复创建；不使用的设备可停用。")
