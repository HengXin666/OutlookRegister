"""Account import mixin for ``HXEmailClient``.

Imports a freshly authorized Outlook account into HX-Email, ensuring a
reuseable account group first. The account-group cache is guarded by
``_account_group_lock`` (owned by the base mixin).
"""

from src.outlookregister.email.hx_email_base import HXEmailError


class _HXEmailImport:
    """Outlook account import + account-group provisioning."""

    def import_outlook_account(
        self,
        email,
        password,
        recovery_email,
        client_id,
        refresh_token,
        proxy_url="",
    ):
        group = self._ensure_account_group(proxy_url)
        imported = self._v1_request(
            "POST",
            "/api/v1/email-accounts/import",
            json={
                "text": f"{email}----{password}----{client_id}----{refresh_token}",
                "provider": "outlook",
                "group_id": group["id"],
                "duplicate_strategy": "overwrite",
                "add_to_pool": False,
            },
            expected=(200, 201),
        )
        if imported.get("failed"):
            raise HXEmailError(f"HX-Email 导入账号失败: {imported.get('errors')}")

        search = self._v1_request(
            "GET",
            "/api/v1/email-accounts/search",
            params={"q": email},
        )
        account = next(
            (
                item for item in search.get("accounts", [])
                if str(item.get("primary_address", "")).lower() == email.lower()
            ),
            None,
        )
        if not account:
            raise HXEmailError("HX-Email 导入后未找到账号")

        remark = (
            f"登录密码: {password}\n"
            f"密保邮箱: {recovery_email or '未触发'}\n"
            "OAuth2: 已授权并导入 refresh_token\n"
            "来源: OutlookRegister 自动注册"
        )
        account_id = account["id"]
        updated = self._v1_request(
            "PUT",
            f"/api/v1/email-accounts/{account_id}",
            json={
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "group_id": group["id"],
                "remark": remark,
                "status": "active",
                "provider": "outlook",
            },
        )
        usable_email = updated.get("primary_usable_email") or {}
        usable_email_id = usable_email.get("id")
        if not usable_email_id:
            raise HXEmailError("HX-Email 账号缺少主可用邮箱 ID")
        try:
            self._v1_request(
                "POST",
                "/api/v1/mail-pool/entries",
                json={"usable_email_id": usable_email_id},
                expected=(200, 201),
            )
        except HXEmailError as exc:
            if exc.status_code != 409:
                raise

        authorization = self._v1_request(
            "POST",
            f"/api/v1/email-accounts/{account_id}/refresh",
        )
        if not authorization.get("success"):
            raise HXEmailError(
                f"HX-Email OAuth2 授权验证失败: {authorization.get('message')}"
            )
        return {
            "account_id": account_id,
            "group_id": group["id"],
            "usable_email_id": usable_email_id,
        }

    def _ensure_account_group(self, proxy_url):
        with self._account_group_lock:
            if self._account_group is not None:
                return self._account_group

            group = self._find_account_group()
            if group is None:
                try:
                    group = self._v1_request(
                        "POST",
                        "/api/v1/groups",
                        json={
                            "name": self.account_group,
                            "color": self.account_group_color,
                            "proxy_url": proxy_url,
                        },
                        expected=(200, 201),
                    )
                except HXEmailError as exc:
                    # HX-Email enforces a unique (user, name) group constraint but
                    # currently exposes a concurrent duplicate as HTTP 500.
                    if exc.status_code not in (409, 500):
                        raise
                    group = self._find_account_group()
                    if group is None:
                        raise

            if not isinstance(group, dict) or not group.get("id"):
                raise HXEmailError("HX-Email 分组响应缺少 ID")
            self._account_group = group
            return group

    def _find_account_group(self):
        groups = self._v1_request("GET", "/api/v1/groups")
        if not isinstance(groups, list):
            raise HXEmailError("HX-Email 分组响应格式无效")
        return next(
            (
                group
                for group in groups
                if isinstance(group, dict)
                and str(group.get("name", "")) == self.account_group
            ),
            None,
        )
