# OutlookRegister  

Outlook 注册机  
选择器经常更新，不保证时效性，自行测试。 

- 模拟人类填表操作  
- 自动过验证码  
- 注册成功  

设置相关：  
1.playwright使用性较差,如果使用playwright，则需要自行寻找指纹浏览器并填写绝对路径。  
2.如果使用patchright,且不需要Oauth2，则只需要更改代理地址.  
3.`Bot_protection_wait`单位为秒。  
4.`client_id`与`redirect_url`可以前往[Azure](https://azure.microsoft.com/zh-cn?OCID=cmmyhidqdn5_brandzone__EFID__)注册获取，不需要Oauth2可留空。  
5.`client_id`与`redirect_url`格式通常类似于`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`和`http://localhost:8000`。  
6.`Scopes`按照申请的权限填，不需要Oauth2可留空。  

使用教程：  
1.先复制安全模板：`cp config.json.example config.json`。`config.json`和`Results/`只保存在本地，分别用于运行配置和账号、令牌、日志等运行产物，不要使用`git add -f`提交。  
2.使用本地代理IP**搭建代理池**，在`config.json`填写你的代理地址。  
3.在设置中调整并发与最大注册量。  
4.如果你需要Oauth2，请在`config.json`中修改`"enable_oauth2"`的值为`true`并填写`Scopes`、`client_id`与`redirect_url`。  
5.安装相关依赖`pip install -r requirements.txt`，如果未安装相关浏览器，使用`patchright install chromium`。  
6.视运行脚本填写或留空`browser_path`。  
7.`python main.py`。  

HX-ProxyGroup 住宅代理对接（托管 VLESS WebSocket）：
1. 启动 HX-ProxyGroup（`./run.sh`，默认控制面 `http://127.0.0.1:19090`），登录后左侧边栏进入「住宅代理」。
2. 「供应商」页：先创建住宅代理供应商——BestProxy 预设已内置官方语法，或选择「API 提取」模式直接填 BestProxy 提取链接（`https://bestproxy.com/api/v2/<提取ID>?app_key=...`，无需账号密码）。保存后用「测试连接」确认能取到出口 IP。
3. 「渠道」页：创建 **sticky** 渠道并设置不小于 `concurrent_flows` 的节点数量。渠道固定发布 VLESS over WebSocket，不开放公网 HTTP/SOCKS 端口。
4. 在`config.json`的`proxy_rotation.control_url`填写渠道的 `https://.../ctl/<control-token>` 地址，并设置 `mihomo_path`；无需填写内部端口、WS Path、UUID 或 HTTP/SOCKS 代理地址。
5. 将`"enabled"`和`"session_scoped"`设为`true`，同时保留`"check_proxy": true`、`"enforce_unique_exit_ip": true`和`"verify_browser_exit_ip": true`。程序从 `/ctl/` 租用声明节点，并为每个活动 flow 启动一个只监听环回地址的本机 Mihomo。
6. 浏览器、密保和 OAuth token 交换始终复用同一声明节点；flow 结束后关闭本机 Mihomo 并归还本地租约，不删除服务端节点。
7. 关闭`proxy_rotation`（`"enabled": false`）时仍使用`config.json`中的静态`proxy`。
8. `check_proxy`会通过代理请求`exit_ip_endpoint`确认出口；启用`enforce_unique_exit_ip`后，活动窗口检测到相同出口 IP 会直接拒绝，不会让两个任务并行使用同一出口。

顶层`proxy`仅在未启用代理池时作为静态回退；启用代理池后，注册、OAuth 浏览器和 token 交换都会使用当前 flow 的代理租约。HX-Email 的控制 API 仍访问本地配置的服务地址，导入账号时会为每个 flow 使用独立分组；分组使用的是 `recovery_email.hx_email.proxy_url` 持久代理，不会写入流程结束即释放的临时 session。

并行会话与切流说明：每个注册 flow 独占 `/ctl/` 返回的声明节点，并由本机 Mihomo 将 VLESS WS 落地为环回 HTTP 代理。注册、密保和 OAuth 会贯穿使用同一个 flow 代理；只有浏览器流程全部结束后才会执行`post_registration_route`切换。默认使用`direct`，避免在 Microsoft 流程中途切换出口。`direct`表示 HX-ProxyGroup 服务器物理出口，`upstream`表示住宅供应商配置的普通上游代理组。请将渠道节点数量和供应商`max_concurrent_sessions`都设置为不小于住宅并发数。

备用邮箱与 OAuth2：
1. 在 `recovery_email.hx_email` 中配置 HX-Email 地址及认证信息。推荐同时配置 `api_key`、`username`、`password`；也可通过 `HX_EMAIL_API_KEY`、`HX_EMAIL_USERNAME`、`HX_EMAIL_PASSWORD` 环境变量提供，避免把凭据写入文件。`proxy_url` 仅填写 HX-Email 服务长期可访问的持久代理，不要填写注册 flow 的临时 session 代理。
2. Microsoft 出现「让我们来保护你的帐户」时，程序会从 HX-Email 申请临时邮箱、等待六位安全码并自动确认。验证码被拒绝时会请求新代码，且不会复用错误代码；`max_code_attempts` 控制最多提交次数。OAuth 登录再次要求确认备选邮箱时，程序会提交同一个已验证地址，并从对应 HX-Email 临时邮箱读取新的安全代码。成功或失败后会结束临时邮箱任务；具备登录凭据时同时归档邮箱。
3. 当前 HX-Email 的通用外部验证码接口尚不能读取独立临时邮箱，因此仅配置 API Key 时需先在 HX-Email 补齐该接口；配置用户名和密码后，本程序会自动回退到 `/api/v1/temp-mail/{id}/codes`。
4. 注册浏览器、OAuth2 授权浏览器及 token 交换使用同一个 flow 代理租约；顶层`proxy`仅在未启用代理池时作为静态回退。
5. Graph 收发信必须使用 HX-Email 相同的 OAuth2 配置：`tenant=consumers`、`prompt=consent`，scope 为 `offline_access Mail.Read Mail.Send`。scope 变化后必须重新授权，旧 refresh token 不会自动获得权限。
6. 每个完成注册或已进入密保绑定步骤的账号都会追加到 `Results/recovery_email_status.jsonl`。`bound=true` 表示验证码已被 Microsoft 明确接受；`bound=false` 时通过 `reason` 和 `detail` 区分未触发、未启用及验证码失败。
7. 账号密码生成后会立即写入 `Results/account_checkpoints.jsonl`，并在资料提交、确认注册、密保失败等阶段持续追加检查点。确认账号已创建后，账号密码会立刻写入原有的 `logged_email.txt` 或 `unlogged_email.txt`；之后即使密保、邮箱初始化、OAuth2 或导入失败，也不会丢失基础账号凭证。

注意事项：  
**IP**与成功率高度正相关，同一IP短时间不宜多次注册。
邮箱自动存储到工作目录的`Results`下。  

任务状态面板：
1. 首次构建前端：`cd dashboard && npm install && npm run build`。
2. 在项目根目录启动面板：`uv run uvicorn dashboard_server:app --host 127.0.0.1 --port 8765`。
3. 浏览器打开 `http://127.0.0.1:8765/`，可查看四个完成状态、账号筛选、阶段耗时和平均耗时。账号详情中的“补充授权”和“加入 HX-Email”可继续处理已注册但后续阶段失败的账号；操作在服务端后台线程执行，页面不会读取账号密码或 token。
4. 新增的 `Results/traffic_usage.jsonl` 会按住宅注册、注册后初始化、密保验证、OAuth 和 HX-Email API 记录观测流量；历史检查点没有流量字段，需重新运行任务后才会显示。

流量是程序观测到的网络字节，不等同于代理供应商账单流量；浏览器优先使用 CDP 统计响应字节，不支持时会使用响应头估算。新记录还会保存 flow ID、代理 session ID 和预检出口 IP，便于核对并行任务是否串用身份。每次按压验证码尝试另写入 `Results/captcha_attempts.jsonl`，可按 flow、session 和出口 IP 对照尝试次数。

浏览器启动默认启用`prevent_direct_network_leaks`，限制非代理 WebRTC UDP 并关闭 QUIC；`verify_browser_exit_ip`还会分别验证注册页和 OAuth 页的真实浏览器出口。`strict_isolation`开启时，缺少 session-scoped 代理、唯一出口校验或独立 HX-Email 分组会直接停止启动。上述设置用于减少直连路径，不代表浏览器指纹会自动随机化。当前实现为注册页和 OAuth 页提供独立浏览器进程、Context、Cookie 和代理会话；同一个 worker 内两者共享同步 Playwright runtime，以避免在仍存活的 runtime 内重复启动 asyncio loop。实现不承诺每个窗口拥有不同的 Canvas、WebGL、字体或硬件指纹。
