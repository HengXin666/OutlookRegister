"""稳定的 Outlook 页面状态到保活步骤提示映射。"""

LOGIN_STEP_MESSAGES = {
    "email_form": ("email_login", "正在填写 Outlook 登录邮箱"),
    "login_form": ("email_login", "正在填写 Outlook 登录密码"),
    "recovery_email_form": ("email_code", "正在填写密保邮箱"),
    "locked": ("unlock", "检测到账号锁定页，准备点击继续"),
    "px_challenge": ("manual_challenge", "检测到按压验证，等待人工处理"),
    "verify_needed": ("manual_challenge", "检测到需要人工处理的安全验证"),
    "sms_verify": ("email_code", "正在处理密保邮箱安全代码"),
    "fido_setup": ("email_login", "正在处理登录选项"),
    "net_error": ("login", "Outlook 登录页出现网络错误，准备重试"),
    "error_page": ("login", "Outlook 登录页返回错误，准备重试"),
    "unknown": ("login", "正在识别 Outlook 登录页面"),
}
