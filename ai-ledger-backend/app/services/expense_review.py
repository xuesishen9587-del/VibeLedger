"""Deterministic expense text for clients; no AI calls or financial decisions."""

INSTALLMENT_NOTICE = "检测到分期，请在网页端处理。"


def text(value, fallback="未识别"):
    return " ".join(str(value).split()) if value is not None and str(value).strip() else fallback


def summary(draft, *, saved=False):
    mode = draft.get("payment_mode")
    if mode == "installment":
        periods = draft.get("total_periods")
        payment = f"分期：{periods}期" if periods else "分期：期数未识别"
    elif mode == "one_off":
        payment = "分期：非分期"
    else:
        payment = "付款方式：未识别"
    lines = ["已保存" if saved else "请核对本笔消费",
        f"金额：{text(draft.get('original_amount'))} {text(draft.get('original_currency'), '币种未识别')}",
        f"商户：{text(draft.get('merchant'))}",
        f"分类：{text((draft.get('category') or {}).get('name'))}",
        f"支付账户：{text((draft.get('from_account') or {}).get('name'))}",
        f"日期：{text(draft.get('occurred_on'))}", payment]
    if draft.get("remarks"):
        lines.append(f"备注：{text(draft['remarks'])}")
    codes = {w["code"] for w in draft.get("warnings", [])}
    if "POSSIBLE_DUPLICATE" in codes:
        lines.append("可能已记过；确认保存会另外记录一笔，请先核对。")
    if codes & {"UNCERTAIN_AMOUNT", "UNCERTAIN_CURRENCY", "UNCERTAIN_DATE", "EXPENSE_INTENT_UNCERTAIN"}:
        lines.append("识别结果需要核对，请确认金额、币种、日期和消费性质。")
    if "CATEGORY_UNCERTAIN" in codes:
        lines.append("分类暂用当前兜底分类，可补充购买用途。")
    if "MISSING_ACCOUNT" in codes:
        lines.append("支付账户未识别，可补充银行名称、卡尾号，或稍后在网页补充。")
    if mode == "installment" and not saved:
        lines.append(INSTALLMENT_NOTICE)
    return "\n".join(lines)


def presentation(draft):
    ready = draft.get("confirmation_ready") is True
    if draft.get("payment_mode") == "installment":
        prompt, action = INSTALLMENT_NOTICE, "review_in_web"
    elif draft.get("review_in_web"):
        prompt, action = "草稿或引用资料已变化，请在网页核对并处理。", "review_in_web"
    elif "confirmation_ready" not in draft:
        prompt, action = "请重新读取草稿后核对。", "recover"
    else:
        codes = {w["code"] for w in draft.get("warnings", [])}
        labels = []
        for code, label in (("INVALID_AMOUNT", "金额及币种"), ("INVALID_DATE", "日期"),
                            ("PAYMENT_MODE_REQUIRED", "一次性或分期付款"),
                            ("EXPENSE_INTENT_UNCERTAIN", "消费性质"), ("UNCERTAIN_AMOUNT", "金额"),
                            ("UNCERTAIN_CURRENCY", "币种"), ("UNCERTAIN_DATE", "日期")):
            if code in codes and label not in labels:
                labels.append(label)
        parts = ["需要补充或核对：" + "、".join(labels) + "。"] if labels else ["请说明需要修改的信息。"]
        if "MISSING_ACCOUNT" in codes:
            parts.append("支付账户未识别，可输入银行名称和卡尾号。")
        if "CATEGORY_UNCERTAIN" in codes:
            parts.append("可补充购买用途以确定分类。")
        parts.append("可以一句话补充；日期请写明年月日。补充内容只修改草稿，不替代交易备注。")
        prompt, action = "\n".join(parts), "confirm" if ready else "supplement"
    return {"display_summary": summary(draft), "confirmation_ready": ready,
            "next_action": action, "supplement_prompt": prompt}


def terminal_fields():
    return {"confirmation_ready": False, "next_action": "done", "supplement_prompt": ""}
