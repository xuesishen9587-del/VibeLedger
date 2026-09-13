"""Account/category management preserved from the accepted settings workflow."""
import streamlit as st
import pandas as pd
from typing import Dict, Any
from api_client import ConflictError
from settings_controller import (SettingsActionController, MutationModifiedPendingError,
    SLOT_CREATE_ACCOUNT, SLOT_CREATE_ALIAS, SLOT_CREATE_CATEGORY)
from ui_errors import handle_api_error


def render(client):
    settings_ctrl = SettingsActionController()
    col_hdr1, col_hdr2 = st.columns([5, 1])
    with col_hdr1:
        st.title("⚙️ 账户与分类管理")
        st.markdown("通过后端 REST API 维护家庭金融账户、账户别名及收支分类")
    with col_hdr2:
        if st.button("🔄 刷新状态", key="btn_settings_refresh", help="重新加载最新数据，保留尚未确定结果的请求"):
            st.rerun()

    tab_acc, tab_cat = st.tabs(["🏦 账户管理与别名", "🏷️ 收支分类管理"])

    with tab_acc:
        st.subheader("1. 现有账户列表")
        try:
            acc_list = client.list_accounts().get("items", [])
        except Exception as e:
            handle_api_error(e, "加载账户失败")
            acc_list = []

        if acc_list:
            acc_df = []
            for a in acc_list:
                acc_df.append({
                    "账户名称": a["name"],
                    "类型": a["account_type"],
                    "币种": a["currency"],
                    "余额覆盖范围": a.get("balance_scope", "-"),
                    "风险级别": a.get("risk_level") or "-",
                    "开启日期": a.get("opened_on") or "-",
                    "状态": a.get("status", "active")
                })
            st.dataframe(pd.DataFrame(acc_df), use_container_width=True, hide_index=True)

        st.divider()

        # 2. 编辑已有账户
        if acc_list:
            st.subheader("2. 编辑已有账户")
            sel_edit_acc_id = st.selectbox(
                "选择要修改的账户",
                options=[a["id"] for a in acc_list],
                format_func=lambda x: next((f"{a['name']} ({a['status']})" for a in acc_list if a["id"] == x), x)
            )
            target_edit_acc = next((a for a in acc_list if a["id"] == sel_edit_acc_id), None)

            if target_edit_acc:
                if st.button("查看账户修改记录",key="account_history_"+target_edit_acc["id"]):
                    st.write(client.request("GET","/api/v1/history",params={"entity_type":"account","entity_id":target_edit_acc["id"]})["items"])
                acc_action_key = f"edit_acc_{target_edit_acc['id']}"
                if settings_ctrl.is_conflict_reload_required(acc_action_key):
                    st.warning("⚠️ 该账户已被并发操作修改 (ROW_VERSION_CONFLICT)。已重新加载当前版本，请重新确认修改并保存。")

                with st.form("edit_account_form"):
                    e_name = st.text_input("修改账户名称", value=target_edit_acc["name"])
                    e_scope = st.text_input("修改余额覆盖范围", value=target_edit_acc.get("balance_scope", ""))
                    e_opened = st.text_input("开户日期 (YYYY-MM-DD, 选填)", value=str(target_edit_acc.get("opened_on") or ""))
                    if target_edit_acc["account_type"] == "credit":
                        e_risk = None
                        st.caption("信用卡账户不允许设置风险级别 (risk_level=None)")
                    else:
                        risk_choices = ["(保持原样)", "(清除设置)", "very_low", "low", "medium", "high"]
                        e_risk = st.selectbox("修改风险级别", risk_choices)
                    e_stmt = st.checkbox("启用账单导入", value=bool(target_edit_acc.get("statement_import_enabled", False)))
                    btn_update_acc = st.form_submit_button("保存账户修改")

                    if btn_update_acc:
                        payload: Dict[str, Any] = {
                            "expected_version": target_edit_acc.get("row_version", 0)
                        }
                        if e_name.strip() and e_name.strip() != target_edit_acc["name"]:
                            payload["name"] = e_name.strip()
                        if e_scope.strip() and e_scope.strip() != target_edit_acc.get("balance_scope", ""):
                            payload["balance_scope"] = e_scope.strip()
                        if e_opened.strip() and e_opened.strip() != str(target_edit_acc.get("opened_on") or ""):
                            payload["opened_on"] = e_opened.strip()
                        if target_edit_acc["account_type"] != "credit":
                            if e_risk == "(清除设置)":
                                payload["risk_level"] = None
                            elif e_risk != "(保持原样)":
                                payload["risk_level"] = e_risk
                        if e_stmt != bool(target_edit_acc.get("statement_import_enabled", False)):
                            payload["statement_import_enabled"] = e_stmt

                        if len(payload) > 1:
                            try:
                                settings_ctrl.execute_mutation(
                                    action_key=acc_action_key,
                                    operation="patch_account",
                                    payload=payload,
                                    resource_id=str(target_edit_acc["id"]),
                                    mutation_fn=lambda k: client.update_account(target_edit_acc["id"], payload, idempotency_key=k)
                                )
                                st.success("🎉 账户信息已更新！")
                                st.rerun()
                            except ConflictError as ex:
                                if ex.code == "ROW_VERSION_CONFLICT":
                                    st.error("⚠️ 保存冲突 (ROW_VERSION_CONFLICT)：该账户已被其他操作更新。请刷新数据后重新修改。")
                                    st.rerun()
                                else:
                                    handle_api_error(ex, "更新账户失败")
                            except MutationModifiedPendingError as ex:
                                st.error(f"⚠️ 操作异常: {ex}")
                            except Exception as ex:
                                handle_api_error(ex, "更新账户失败")

        st.divider()

        # 3. 创建新账户
        st.subheader("3. 创建新账户")
        with st.form("create_account_form", clear_on_submit=True):
            n_name = st.text_input("账户名称 (如: 工行日常卡)")
            n_type = st.selectbox("账户类型", ["cash", "savings", "credit", "investment"])
            n_scope = st.text_input("余额覆盖范围", value="", help="例如：券商账户总资产，包含现金和持仓；各账户应覆盖互不重叠的资金。")
            n_curr = st.selectbox("币种", ["CNY", "USD", "EUR", "JPY", "SGD"])
            n_opened = st.text_input("开户日期 (YYYY-MM-DD, 选填)", value="")
            if n_type == "credit":
                n_risk = None
                st.caption("信用卡账户不允许设置风险级别 (risk_level=None)")
            else:
                n_risk_sel = st.selectbox("风险级别 (选填)", ["(无)", "very_low", "low", "medium", "high"])
                n_risk = None if n_risk_sel == "(无)" else n_risk_sel
            n_stmt = st.checkbox("启用账单导入", value=False)
            btn_create_acc = st.form_submit_button("创建账户")

            if btn_create_acc:
                if not n_name.strip() or not n_scope.strip():
                    st.error("账户名称和平衡范围为必填项！")
                else:
                    create_payload: Dict[str, Any] = {
                        "name": n_name.strip(),
                        "balance_scope": n_scope.strip(),
                        "account_type": n_type,
                        "currency": n_curr,
                        "statement_import_enabled": n_stmt,
                    }
                    if n_risk:
                        create_payload["risk_level"] = n_risk
                    if n_opened.strip():
                        create_payload["opened_on"] = n_opened.strip()

                    create_acc_action_key = SLOT_CREATE_ACCOUNT
                    try:
                        settings_ctrl.execute_mutation(
                            action_key=create_acc_action_key,
                            operation="create_account",
                            payload=create_payload,
                            mutation_fn=lambda k: client.create_account(
                                name=create_payload["name"],
                                balance_scope=create_payload["balance_scope"],
                                account_type=create_payload["account_type"],
                                currency=create_payload["currency"],
                                idempotency_key=k,
                                risk_level=create_payload.get("risk_level"),
                                opened_on=create_payload.get("opened_on"),
                                statement_import_enabled=create_payload["statement_import_enabled"],
                            )
                        )
                        st.success(f"🎉 账户 {n_name} 创建成功！")
                        st.rerun()
                    except MutationModifiedPendingError as ex:
                        st.error(f"⚠️ 操作异常: {ex}")
                    except Exception as ex:
                        handle_api_error(ex, "创建账户失败")

        # 4. 账户别名管理
        if acc_list:
            st.divider()
            st.subheader("4. 账户别名管理 (快捷指令截图多模态匹配)")
            sel_alias_acc = st.selectbox("选择要管理别名的账户", options=[a["id"] for a in acc_list], format_func=lambda x: next((a["name"] for a in acc_list if a["id"] == x), x))
            try:
                aliases = client.list_account_aliases(sel_alias_acc).get("items", [])
                if aliases:
                    st.write("已配置别名:")
                    for al in aliases:
                        al_col1, al_col2 = st.columns([3, 1])
                        with al_col1:
                            st.write(f"- `{al['alias']}` ({al.get('status', 'active')}, v{al.get('row_version', 0)})")
                        with al_col2:
                            if al.get("status") == "active":
                                if st.button("⛔ 停用别名", key=f"archive_al_{al['id']}"):
                                    alias_action_key = f"archive_alias_{al['id']}"
                                    archive_payload = {
                                        "status": "inactive",
                                        "expected_version": al.get("row_version", 0)
                                    }
                                    try:
                                        settings_ctrl.execute_mutation(
                                            action_key=alias_action_key,
                                            operation="patch_alias_archive",
                                            payload=archive_payload,
                                            resource_id=f"{sel_alias_acc}:{al['id']}",
                                            mutation_fn=lambda k: client.update_account_alias(
                                                account_id=sel_alias_acc,
                                                alias_id=al["id"],
                                                payload=archive_payload,
                                                idempotency_key=k,
                                            )
                                        )
                                        st.success("别名已停用")
                                        st.rerun()
                                    except ConflictError as ex:
                                        if ex.code == "ROW_VERSION_CONFLICT":
                                            st.error("⚠️ 停用冲突 (ROW_VERSION_CONFLICT)：该别名已被更新。请刷新数据后重试。")
                                            st.rerun()
                                        else:
                                            handle_api_error(ex, "停用别名失败")
                                    except MutationModifiedPendingError as ex:
                                        st.error(f"⚠️ 操作异常: {ex}")
                                    except Exception as ex:
                                        handle_api_error(ex, "停用别名失败")
                            else:
                                st.info("已停用")
                else:
                    st.caption("该账户暂无别名。")
            except Exception as e:
                handle_api_error(e, "加载别名失败")

            with st.form("add_alias_form", clear_on_submit=True):
                new_alias_str = st.text_input("新增别名 (如: 工行Visa卡)")
                if st.form_submit_button("添加别名") and new_alias_str.strip():
                    add_alias_key = SLOT_CREATE_ALIAS
                    alias_payload = {"alias": new_alias_str.strip()}
                    try:
                        settings_ctrl.execute_mutation(
                            action_key=add_alias_key,
                            operation="create_alias",
                            payload=alias_payload,
                            resource_id=str(sel_alias_acc),
                            mutation_fn=lambda k: client.create_account_alias(sel_alias_acc, new_alias_str.strip(), idempotency_key=k)
                        )
                        st.success("别名添加成功！")
                        st.rerun()
                    except MutationModifiedPendingError as ex:
                        st.error(f"⚠️ 操作异常: {ex}")
                    except Exception as ex:
                        handle_api_error(ex, "添加别名失败")

    with tab_cat:
        st.subheader("🏷️ 收支分类列表")
        try:
            cats = client.list_categories(status=None).get("items", [])
        except Exception as e:
            handle_api_error(e, "加载分类失败")
            cats = []

        if cats:
            cat_df = pd.DataFrame(cats)
            st.dataframe(cat_df, use_container_width=True, hide_index=True)

        st.divider()

        # 编辑或停用已有分类
        if cats:
            st.subheader("编辑或停用分类")
            sel_edit_cat_id = st.selectbox(
                "选择分类",
                options=[c["id"] for c in cats],
                format_func=lambda x: next((f"{c['name']} ({c['category_type']}, {c['status']})" for c in cats if c["id"] == x), x)
            )
            target_edit_cat = next((c for c in cats if c["id"] == sel_edit_cat_id), None)
            if target_edit_cat:
                if st.button("查看分类修改记录",key="category_history_"+target_edit_cat["id"]):
                    st.write(client.request("GET","/api/v1/history",params={"entity_type":"category","entity_id":target_edit_cat["id"]})["items"])
                cat_action_key = f"edit_cat_{target_edit_cat['id']}"
                if settings_ctrl.is_conflict_reload_required(cat_action_key):
                    st.warning("⚠️ 该分类已被并发操作修改 (ROW_VERSION_CONFLICT)。已重新加载当前版本，请核对并重新保存。")

                c_edit1, c_edit2 = st.columns([3, 1])
                with c_edit1:
                    with st.form("edit_cat_form"):
                        new_cat_name = st.text_input("分类名称", value=target_edit_cat["name"])
                        new_cat_desc = st.text_input("分类描述 (选填)", value=target_edit_cat.get("description") or "")
                        btn_update_cat = st.form_submit_button("保存分类修改")
                        if btn_update_cat:
                            cat_payload: Dict[str, Any] = {
                                "expected_version": target_edit_cat.get("row_version", 0)
                            }
                            if new_cat_name.strip() and new_cat_name.strip() != target_edit_cat["name"]:
                                cat_payload["name"] = new_cat_name.strip()
                            if (new_cat_desc.strip() or None) != target_edit_cat.get("description"):
                                cat_payload["description"] = new_cat_desc.strip() or None

                            if len(cat_payload) > 1:
                                try:
                                    settings_ctrl.execute_mutation(
                                        action_key=cat_action_key,
                                        operation="patch_category",
                                        payload=cat_payload,
                                        resource_id=str(target_edit_cat["id"]),
                                        mutation_fn=lambda k: client.update_category(target_edit_cat["id"], cat_payload, idempotency_key=k)
                                    )
                                    st.success("分类名称已更新！")
                                    st.rerun()
                                except ConflictError as ex:
                                    if ex.code == "ROW_VERSION_CONFLICT":
                                        st.error("⚠️ 保存冲突 (ROW_VERSION_CONFLICT)：该分类已被其他操作更新。请刷新数据后重试。")
                                        st.rerun()
                                    else:
                                        handle_api_error(ex, "更新分类失败")
                                except MutationModifiedPendingError as ex:
                                    st.error(f"⚠️ 操作异常: {ex}")
                                except Exception as ex:
                                    handle_api_error(ex, "更新分类失败")
                with c_edit2:
                    if target_edit_cat.get("status") == "active":
                        if st.button("⛔ 停用分类", key=f"archive_cat_{target_edit_cat['id']}"):
                            archive_cat_key = f"archive_cat_{target_edit_cat['id']}"
                            archive_payload = {
                                "status": "inactive",
                                "expected_version": target_edit_cat.get("row_version", 0)
                            }
                            try:
                                settings_ctrl.execute_mutation(
                                    action_key=archive_cat_key,
                                    operation="patch_category_archive",
                                    payload=archive_payload,
                                    resource_id=str(target_edit_cat["id"]),
                                    mutation_fn=lambda k: client.update_category(target_edit_cat["id"], archive_payload, idempotency_key=k)
                                )
                                st.success("分类已停用")
                                st.rerun()
                            except ConflictError as ex:
                                if ex.code == "ROW_VERSION_CONFLICT":
                                    st.error("⚠️ 停用冲突 (ROW_VERSION_CONFLICT)：该分类已被其他操作更新。请刷新数据后重试。")
                                    st.rerun()
                                else:
                                    handle_api_error(ex, "停用分类失败")
                            except MutationModifiedPendingError as ex:
                                st.error(f"⚠️ 操作异常: {ex}")
                            except Exception as ex:
                                handle_api_error(ex, "停用分类失败")
                    else:
                        st.info("已停用")

        st.divider()
        st.subheader("创建新分类")
        with st.form("create_cat_form", clear_on_submit=True):
            cat_name = st.text_input("分类名称 (如: 餐饮美食, 数码科技)")
            cat_type = st.selectbox("分类性质", ["expense", "income"])
            cat_desc = st.text_input("分类描述 (选填)", value="")
            if st.form_submit_button("创建分类") and cat_name.strip():
                create_cat_key = SLOT_CREATE_CATEGORY
                create_cat_payload = {
                    "name": cat_name.strip(),
                    "category_type": cat_type,
                    "description": cat_desc.strip() if cat_desc.strip() else None
                }
                try:
                    settings_ctrl.execute_mutation(
                        action_key=create_cat_key,
                        operation="create_category",
                        payload=create_cat_payload,
                        mutation_fn=lambda k: client.create_category(
                            name=cat_name.strip(),
                            category_type=cat_type,
                            description=create_cat_payload["description"],
                            idempotency_key=k,
                        )
                    )
                    st.success(f"分类 {cat_name} 创建成功！")
                    st.rerun()
                except MutationModifiedPendingError as ex:
                    st.error(f"⚠️ 操作异常: {ex}")
                except Exception as ex:
                    handle_api_error(ex, "创建分类失败")
