# OutlookRegister  

Outlook 注册机  
选择器经常更新，不保证时效性，自行测试。 

- 模拟人类填表操作  
- 遇到需要人工完成的验证时暂停，并由面板继续
- 注册成功  

设置相关：  
1.playwright使用性较差,如果使用playwright，则需要自行寻找指纹浏览器并填写绝对路径。  
2.推荐使用patchright；批量流程不再切换本地直连或静态代理，而是从 HX-ProxyGroup 声明的住宅节点池租用固定节点。
3.`Bot_protection_wait`单位为秒。  
4.`client_id`与`redirect_url`可以前往[Azure](https://azure.microsoft.com/zh-cn?OCID=cmmyhidqdn5_brandzone__EFID__)注册获取，不需要Oauth2可留空。  
5.`client_id`与`redirect_url`格式通常类似于`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`和`http://localhost:8000`。  
6.`Scopes`按照申请的权限填，不需要Oauth2可留空。  

使用教程：  
1.先复制安全模板：`cp config.json.example config.json`。`config.json`和`Results/`只保存在本地，分别用于运行配置和账号、令牌、日志等运行产物，不要使用`git add -f`提交。  
2.不要配置顶层静态`proxy`，也不需要填写国家、浏览器语言、时区、token 或 Listener。在面板的「运行配置」中只粘贴完整的 HX-ProxyGroup 住宅控制 URL（`https://主机/ctl/<control-token>`），点击「校验并启用」；校验成功后程序会自动保存全部住宅配置。
3.在面板的「工作流」页提交批量注册或批量保活；批量操作统一从面板启动，不直接使用脚本作为批量入口。
4.如果你需要Oauth2，请在配置页启用`oauth2.enable_oauth2`并填写`Scopes`、`client_id`与`redirect_url`。
5.安装相关依赖`pip install -r requirements.txt`，如果未安装相关浏览器，使用`patchright install chromium`。  
6.视运行环境填写或留空`browser_path`。

HX-ProxyGroup 住宅代理对接（动态住宅 IP）：
1. 启动 HX-ProxyGroup。在「代理服务」页面找到对应住宅渠道服务，选择“复制自动化控制 URL”获得完整 HTTPS 地址（`https://主机/ctl/<control-token>`）；该 URL 是唯一需要在本项目面板中填写的住宅配置。
2. 「供应商」页：先创建住宅代理供应商——BestProxy 预设已内置官方语法，或选择「API 提取」模式直接填 BestProxy 提取链接（`https://bestproxy.com/api/v2/<提取ID>?app_key=...`，无需账号密码）。保存后用「测试连接」确认能取到出口 IP。
3. 「渠道」页：选择 **sticky** 模式，设置不小于 OutlookRegister 并发数的「会话数量」，再按部署选择 VLESS、VMess 或 Trojan WebSocket。HX-ProxyGroup 只向公网发布经过 CF/雷池的 WS 入口，不需要为浏览器额外开放 HTTP、SOCKS5 或 Mixed 直连端口。
4. 点击校验后，程序调用 `GET /ctl/<token>/nodes` 读取固定节点池，从进程内租用一个空闲节点，再调用该节点的 `/next` 刷新服务端内部住宅出口。节点名、代理账号和入口保持不变，普通用户无需看到供应商会话或出口 IP 的轮换细节。
5. 程序优先使用返回的浏览器兼容 `proxy_url`；纯 WS 渠道的 `proxy_url` 为 `null` 时，会从 `endpoints[]` 选择 VLESS/VMess/Trojan WS URI，自动启动短生命周期的本机 Mihomo，并把环回 HTTP 代理交给 Playwright。随后程序通过固定 HTTPS 地理探针确认出口 IP、国家代码和 IANA 时区，并据此自动选择浏览器语言和时区。
6. 校验成功会自动启用住宅路由、出口检查、活动出口 IP 去重、浏览器出口复核和防直连开关，并保存配置。
7. 每个 flow 独占一个声明节点；flow 结束只归还 OutlookRegister 进程内租约，不删除服务端节点。节点池全部占用时会明确报错，应降低并发或增加渠道会话数。
8. 校验失败不会保存新的控制 URL；只有控制接口、代理入口、住宅出口和国家/时区确认全部通过时才显示成功。

控制 URL 是 bearer 凭据，不是普通公开链接。它应只通过 HTTPS 使用，不要放进截图、工单、浏览器分享或公共日志；若怀疑泄露，应在 HX-ProxyGroup 管理页轮换控制令牌并同步更新配置。客户端不会跟随控制面重定向，也不会在错误日志中输出完整 URL 或 token。

顶层`proxy`不参与严格动态住宅运行。启用住宅节点池后，注册、密保、OAuth 浏览器和 token 交换都会使用当前 flow 的节点租约；同一个 flow 从头到尾固定节点、国家、浏览器语言、时区和出口 IP，另一个 flow 才会租用其他空闲节点。选中的国家、语言、时区、节点和出口 IP 会写入检查点及流量记录。HX-Email 控制 API 仍访问配置的服务地址，导入账号使用独立分组；控制面本身不会进入 HX-ProxyGroup 的实际代理转发路径。

并行租约与切流说明：每个 flow 开始时从 `GET /ctl/<token>/nodes` 的声明节点中租用一个空闲项，并调用 `POST /ctl/<token>/nodes/<index>/next` 换出站 IP；随后通过节点的浏览器兼容入口或由 `endpoints[]` 启动的本机 Mihomo 环回入口确认出口 IP、国家和时区，再固定对应浏览器身份。注册、密保、OAuth 和完成后的 HX-Email 导入贯穿使用同一个 flow 身份；`post_registration_route`固定为`residential`。请同时保证渠道`session_count`和供应商`max_concurrent_sessions`不小于住宅并发数。可用 `HX_MIHOMO_BIN` 指定 Mihomo 可执行文件；每个本地实例只监听随机 `127.0.0.1` 端口，并在 flow 释放后停止。旧版`rotation_url`以及分离式`base_url`、`tokens`仍兼容，但不再是推荐配置。

完全注册流程：面板提交任务后依次执行「注册」→「填写密保邮箱」→「获取 OAuth 授权」→「加入 HX-Email」。每个阶段会写入检查点；后续阶段失败不会丢失已生成的账号凭据。

完全保活流程：在面板选择账号和「账号密码登录」或「密保邮箱取件登录」→如出现按压/人工验证，浏览器保持打开并在面板点击「继续」→如缺少授权则补充 OAuth→如配置启用则加入 HX-Email。整个流程复用同一个住宅节点租约，人工验证超时后需要重新提交该账号的保活任务。

保活的“如有”判定基准：
1. 人工验证：页面状态机检测可见的`#px-captcha`、`hsprotect`/验证 iframe 或多语言安全挑战文本；点击面板“继续”后会重新扫描，挑战仍在时不会继续后续阶段。
2. 补充授权：先读取本地 refresh token；默认通过当前 HX-ProxyGroup flow 向 Microsoft token endpoint 做一次实际 refresh 探针。缺失、空值或探针返回`invalid_grant`等失败时，才使用当前已登录浏览器会话补充 OAuth/Graph 授权，并在失败后尝试同一 flow 的独立浏览器 Context。探针和 token 交换都关闭系统代理环境继承。
3. 加入 HX-Email：只有拿到可用 refresh token 且`keepalive.auto_import_hx_email=true`才执行。HX-Email 导入完成后会重新查询账号、写入账号信息和邮件池，并调用 refresh 接口验证授权；这些步骤全部成功后才写入`hx_email_imported`检查点。

与 `reg-factory` 的对应关系：其“解锁”部分对应这里的登录状态机，其“提取 Graph”对应这里的浏览器 OAuth/Graph 授权和 refresh 探针，其账号池回写对应这里的检查点、`outlook_token.txt`和 HX-Email 导入。没有复制它的 EZCaptcha 自动绕过、Clash/direct 回退或不受国家约束的代理池；当前 flow 始终由 HX-ProxyGroup 固定国家、声明节点和出口 IP。

备用邮箱与 OAuth2：
1. 在 `recovery_email.hx_email` 中配置 HX-Email 地址及认证信息。推荐同时配置 `api_key`、`username`、`password`；也可通过 `HX_EMAIL_API_KEY`、`HX_EMAIL_USERNAME`、`HX_EMAIL_PASSWORD` 环境变量提供，避免把凭据写入文件。`proxy_url` 仅填写 HX-Email 服务长期可访问的持久代理，不要填写注册 flow 当前租用的住宅节点代理。
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
3. 浏览器打开`http://127.0.0.1:8765/`，在「工作流」页执行批量注册/保活，在「运行配置」页修改并持久化配置。配置保存后通过 SSE 实时通知其他面板窗口，新任务会读取新版本。
4. 账号详情中的“补充授权”“加入 HX-Email”和“保活登录”可继续处理单个账号；操作在服务端后台线程执行，页面不会读取账号密码或 token。
5. 新增的`Results/traffic_usage.jsonl`会按住宅注册、密保验证、OAuth 和 HX-Email API 记录观测流量；历史检查点没有流量字段，需重新运行任务后才会显示。

流量是程序观测到的网络字节，不等同于代理供应商账单流量；浏览器优先使用 CDP 统计响应字节，不支持时会使用响应头估算。新记录还会保存 flow ID、代理 session ID 和预检出口 IP，便于核对并行任务是否串用身份。每次按压验证码尝试另写入 `Results/captcha_attempts.jsonl`，可按 flow、session 和出口 IP 对照尝试次数。

浏览器启动默认启用`prevent_direct_network_leaks`，限制非代理 WebRTC UDP 并关闭 QUIC；`verify_browser_exit_ip`还会分别验证注册页和 OAuth 页的真实浏览器出口。`strict_isolation`开启时，缺少 session-scoped 代理、唯一出口校验或独立 HX-Email 分组会直接停止启动。上述设置用于减少直连路径，不代表浏览器指纹会自动随机化。当前实现为注册页和 OAuth 页提供独立浏览器进程、Context、Cookie 和代理会话；同一个 worker 内两者共享同步 Playwright runtime，以避免在仍存活的 runtime 内重复启动 asyncio loop。实现不承诺每个窗口拥有不同的 Canvas、WebGL、字体或硬件指纹。
