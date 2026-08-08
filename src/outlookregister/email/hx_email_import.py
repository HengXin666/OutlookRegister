"""Account import mixin for ``HXEmailClient``.

Places a freshly authorized Outlook account into HX-Email, ensuring a reusable
account group first. Registration always imports; keepalive upserts, so a
returning account is updated in place instead of re-imported. The account-group
cache is guarded by ``_account_group_lock`` (owned by the base mixin).

Group proxy policy: the account group is only ever bound to the configured
group proxy (default ``http://127.0.0.1:2334``). The residential/browser proxy
lease must never leak into HX-Email, otherwise mailbox/refresh traffic is
billed through the expensive residential pool and refresh failures can leave
accounts in a broken state.
"""

from outlookregister.email.hx_email_base import HXEmailError

DEFAULT_GROUP_PROXY_URL = "http://127.0.0.1:2334"


class _HXEmailImport:
    """Outlook account import/update + account-group provisioning."""

    def import_outlook_account(
        self,
        email,
        password,
        recovery_email,
        client_id,
        refresh_token,
        proxy_url="",
        stage="register",
    ):
        """Import a newly registered account into its configured group."""
        return self.upsert_outlook_account(
            email=email,
            password=password,
            recovery_email=recovery_email,
            client_id=client_id,
            refresh_token=refresh_token,
            proxy_url=proxy_url,
            stage=stage,
            reuse_existing=False,
        )

    def upsert_outlook_account(
        self,
        email,
        password,
        recovery_email,
        client_id,
        refresh_token,
        proxy_url="",
        stage="keepalive",
        reuse_existing=True,
    ):
        """Update the account when the group already holds it, else import it.

        ``reuse_existing`` is what separates keepalive from registration: a
        keepalive run must not create a second copy of an account the group
        already tracks, while registration always starts from an import.
        """
        # Empty/omitted group proxy always falls back to the stable group
        # proxy instead of the residential lease. Callers must not pass the
        # browser proxy here.
        proxy_url = str(proxy_url or "").strip() or DEFAULT_GROUP_PROXY_URL
        group = self._ensure_account_group(proxy_url, stage=stage)
        group_id = group["id"]

        account = self._find_group_account(email, group_id) if reuse_existing else None
        mode = "updated" if account else "imported"
        if account is None:
            self._import_account_text(email, password, client_id, refresh_token, group_id)
            account = self._find_group_account(email, group_id, any_group=True)
            if not account:
                raise HXEmailError("HX-Email 导入后未找到账号")

        account_id = account["id"]
        updated = self._v1_request(
            "PUT",
            f"/api/v1/email-accounts/{account_id}",
            json={
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "group_id": group_id,
                "remark": self._account_remark(password, recovery_email, stage, mode),
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
            "group_id": group_id,
            "usable_email_id": usable_email_id,
            "mode": mode,
        }

    def _import_account_text(self, email, password, client_id, refresh_token, group_id):
        imported = self._v1_request(
            "POST",
            "/api/v1/email-accounts/import",
            json={
                "text": f"{email}----{password}----{client_id}----{refresh_token}",
                "provider": "outlook",
                "group_id": group_id,
                "duplicate_strategy": "overwrite",
                "add_to_pool": False,
            },
            expected=(200, 201),
        )
        if imported.get("failed"):
            raise HXEmailError(f"HX-Email 导入账号失败: {imported.get('errors')}")
        return imported

    def _find_group_account(self, email, group_id, any_group=False):
        """Return the account for ``email``, optionally restricted to a group."""
        search = self._v1_request(
            "GET",
            "/api/v1/email-accounts/search",
            params={"q": email},
        )
        target = str(email or "").strip().lower()
        for item in search.get("accounts", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("primary_address", "")).strip().lower() != target:
                continue
            if not any_group and item.get("group_id") != group_id:
                continue
            if item.get("id"):
                return item
        return None

    @staticmethod
    def _account_remark(password, recovery_email, stage, mode):
        source = "OutlookRegister 自动保活" if stage == "keepalive" else "OutlookRegister 自动注册"
        action = "分组内已存在，已更新" if mode == "updated" else "新增导入"
        return (
            f"登录密码: {password}\n"
            f"密保邮箱: {recovery_email or '未触发'}\n"
            "OAuth2: 已授权并导入 refresh_token\n"
            f"来源: {source}（{action}）"
        )

    def _ensure_account_group(self, proxy_url, stage=""):
        group_name = self.group_name_for_stage(stage)
        proxy_url = str(proxy_url or "").strip() or DEFAULT_GROUP_PROXY_URL
        with self._account_group_lock:
            cached = self._account_groups.get(group_name)
            if cached is not None:
                return cached

            existing = self._find_account_group(group_name)
            if existing is None:
                try:
                    group = self._v1_request(
                        "POST",
                        "/api/v1/groups",
                        json={
                            "name": group_name,
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
                    group = self._find_account_group(group_name)
                    if group is None:
                        raise
            else:
                # Self-heal a stale group proxy (e.g. an earlier run leaked a
                # residential lease) back to the configured group proxy. Only
                # our own group name is touched, so unrelated groups are
                # unaffected; a freshly created group already carries it.
                group = existing
                if str(group.get("proxy_url") or "").strip() != proxy_url:
                    group = self._v1_request(
                        "PUT",
                        f"/api/v1/groups/{group['id']}",
                        json={
                            "name": group_name,
                            "color": self.account_group_color,
                            "proxy_url": proxy_url,
                        },
                    )
                    if not isinstance(group, dict) or not group.get("id"):
                        raise HXEmailError("HX-Email 分组更新响应缺少 ID")

            if not isinstance(group, dict) or not group.get("id"):
                raise HXEmailError("HX-Email 分组响应缺少 ID")
            self._account_groups[group_name] = group
            return group

    def _find_account_group(self, group_name=""):
        target = str(group_name or self.account_group)
        groups = self._v1_request("GET", "/api/v1/groups")
        if not isinstance(groups, list):
            raise HXEmailError("HX-Email 分组响应格式无效")
        return next(
            (
                group
                for group in groups
                if isinstance(group, dict) and str(group.get("name", "")) == target
            ),
            None,
        )
