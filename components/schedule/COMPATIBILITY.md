# AstrBot 兼容性核对记录

本阶段以 AstrBot 官方 `v4.26.0` 和 `v4.29.4` tag 源码核对接口。开发时官方 `releases/latest` 指向 `v4.29.4`。本地测试使用 AstrBot mock；真实 AstrBot 加载、面板显示、QQ 收发及模型效果仍需按需求文档第 17.2 节验收。

官方源码：[v4.26.0](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.0)、[v4.29.4](https://github.com/AstrBotDevs/AstrBot/tree/v4.29.4)。以下行号来自 `v4.26.0`。

| 用到的接口 | 源码位置与签名 |
| --- | --- |
| 请求前钩子 | `astrbot/core/star/register/star_handler.py:427`，`@filter.on_llm_request()`，回调 `(event, req)` |
| 模型回复钩子 | 同文件 `:450`，`@filter.on_llm_response()`，回调 `(event, response)`；此时消息尚未发送 |
| 发送后钩子 | 同文件 `:738`，`@filter.after_message_sent()`，回调 `(event)`；`core/pipeline/respond/stage.py:292` 调用，随后清空 result |
| 指令和工具 | `filter.command(name, alias=set())`、`filter.llm_tool(name=...)`；`astrbot/api/event/filter/__init__.py` 公开导出 |
| 剩余参数 | `astrbot/core/star/filter/command.py:15` 的 `GreedyStr`；4.26 签名解析不展开字符串注解，因此入口使用实际类型或 `= GreedyStr` 默认值 |
| 管理员检查 | `astrbot/core/platform/astr_message_event.py:262` 的 `event.is_admin()` |
| 指定模型 | `astrbot/core/star/context.py:384` 的同步 `get_provider_by_id(provider_id)` |
| 默认模型 | 同文件 `:424` 的同步 `get_using_provider(umo=None)` |
| 人设 | `astrbot/core/persona_mgr.py:47` 的同步 `get_persona_v3_by_id(id)`，返回含 `prompt` 的 dict；`:40` 的异步 `get_persona(id)` 返回含 `system_prompt` 的 DB 对象；`:63` 的异步 `get_default_persona_v3(umo=None)` |
| 历史 | `astrbot/core/conversation_mgr.py:160` 的异步 `get_curr_conversation_id(umo)`、`:176` 的 `get_conversation(umo, cid, create_if_not_exists=False)`；`history` 为 JSON 字符串 |
| 临时会话清理 | 同文件 `:125` 的异步 `delete_conversation(umo, cid=None)` |
| 模型调用 | `astrbot/core/provider/provider.py` 的异步 `text_chat(prompt, session_id=None, image_urls=None, contexts=None, ...)`；调用使用独立随机会话和空上下文 |
| 独立数据目录 | `astrbot/core/star/star_tools.py:206` 的 `StarTools.get_data_dir(plugin_name=None) -> Path` |
| HTML 卡片 | `astrbot/core/star/base.py:66` 的异步 `html_render(tmpl, data, return_url=True, options=None)` |
| 临时注入 | `astrbot/core/agent/message.py:68` 的 `TextPart.mark_as_temp()`；请求的 `extra_user_content_parts` 存在于 `core/provider/entities.py:98` |
| 时区 | `context.get_config().get("timezone")`，缺省回退 `Asia/Shanghai` |
| 配置面板 | `dashboard/src/components/shared/ConfigItemRenderer.vue` 已支持 `select_persona`、`select_provider`；`TemplateListEditor.vue:320` 创建的条目包含 `__template_key` |

## 指令路由实测

将两版官方 `CommandFilter` 独立加载，以 mock 消息执行匹配和参数解析：

- 两版都支持 `alias={"life show"}` 这种带空格的普通指令别名。
- 独立注册 `life profile` 与 `life profile regen` 会产生前缀重叠：前者也会匹配后者，且无参数命令忽略多余参数。
- 使用单个 `life` 入口和 `GreedyStr` 分发英文子命令，`life profile regen` 在两版都正确得到 `"profile regen"`。
- 主人配置允许普通账号，所以主人操作使用 `owner_ids` / `event.is_admin()` 自检，避免 ADMIN 装饰器提前拦截主人。

## 可选能力降级

临时内容注入通过 `ImportError` / `hasattr` 检测，缺失时回退 system prompt，并只记录一次 info。缺少人设接口时使用生活档案；请求失败、无模型及超时由后台生成状态和重试处理。HTML 衣柜卡片渲染失败回退文字。

## Python 与依赖

插件代码目标 Python 3.10。`requirements.txt` 使用兼容范围；本地验证环境安装 `APScheduler 3.11.0`、`aiohttp 3.13.2`、`Pillow 12.0.0`、`tzdata 2025.2`，这些版本的官方发行元数据均覆盖 Python 3.10。`chinesecalendar 1.11.0` 和 `lunar_python 1.4.8` 的官方源码包全部 Python 文件已使用 `ast.parse(feature_version=(3, 10))` 检查通过；其中节假日数据覆盖到 2026 年，超范围回退周末规则。

依赖信息：[APScheduler](https://pypi.org/project/APScheduler/3.11.0/)、[aiohttp](https://pypi.org/project/aiohttp/3.13.2/)、[Pillow](https://pypi.org/project/Pillow/12.0.0/)、[tzdata](https://pypi.org/project/tzdata/2025.2/)、[chinesecalendar](https://pypi.org/project/chinesecalendar/1.11.0/)、[lunar_python](https://pypi.org/project/lunar_python/1.4.8/)。

## 配置面板补丁：插件视图与原生图片上传

两版官方 `Context.register_web_api(route, view_handler, methods, desc)` 均支持注册固定自家路由 `/<插件ID>/wardrobe`。前端通过 `AstrBotPluginView || AstrBotPluginPage` 的 `ready()` / `apiGet("wardrobe")` 访问，由宿主桥转发认证请求；页面没有直接读取 localStorage 或 JWT。后端仅投影衣柜字段及本地展示图 data URI。

两版配置面板 `FileConfigItem.vue` 支持 `type: "file"`、数组默认值和 `file_types`，可放入 template_list 模板。原生上传保存相对 `files/...` 路径，插件按自身数据目录解析并限制在 files 子目录；不接受外部 URL 或目录穿越。配置保存后框架重载插件，故在后台 bootstrap 扫描已保存条目，无需新增聊天入口或配置保存 hook。内容哈希用于跳过相同图片，支持同路径文件被覆盖后重新提取。

框架同一路由重载时替换 handler，卸载时没有自动注销；本插件在 terminate 中只移除归属当前实例的 handler，避免残留旧实例或误删新实例。接口权限依赖宿主 extensions API 的标准认证，未额外声明逐插件 token 隔离能力。

参考源码位置（两版均存在）：`astrbot/core/star/context.py`、`astrbot/api/web.py`、`astrbot/dashboard/services/plugin_service.py`、`dashboard/src/components/shared/FileConfigItem.vue` 及插件视图 SDK。真实面板上传、保存重载和模型转写端到端仍需在 AstrBot 实例验收。

## 四板块视图升级（当前）

新入口 `pages/life/index.html` 及相对 `app.js/style.css/api.js` 使用两版已有的标准页面资源能力；metadata.yaml 的 pages 使用宿主支持的 list[dict] 声明，name 为 life、entry_file 为 index.html；实际文件位于 pages/life/index.html。

注册 14 个固定路由：衣柜 list/add/edit/delete/transcribe/collect、schedule/today 和 schedule/retry、profile/get/save/regenerate、settings/get/save，以及 tasks/get。方法按指令使用 GET/POST，handler 无位置参数，通过 `astrbot.api.web.request.json()` 异步读取正文。后台进度用 `apiGet("tasks/get", {id})` 传查询参数，不把查询串拼到 endpoint。

Dashboard 为配置中的单一管理员账号，没有公开 role/is_admin 字段。写接口将宿主已认证的 request.username 与当前 dashboard.username 比对，拒绝 api_key: 身份，并检查宿主已验签凭据的 token_type，额外排除页面资源 token；插件不会自行替代宿主 JWT 验签。聊天 owner_ids 与管理员 Web 权限分开处理。

模型列表使用同步 `context.get_all_providers()`，仅投影 `provider.meta().id` 与模型名称，不返回 provider_config。保存设置后调用 config.save_config；生成时间变化立即重新排期。

两版 bridge.apiPost 只有 endpoint/body 参数，没有 SDK 超时选项。宿主普通 POST 未设置 Axios 默认超时；本插件通用 POST 等待上限仍为 210 秒。档案生成和衣柜图片转写均先受理并返回后台任务 ID，再用 tasks/get 查询进度；每次查询最多等待 20 秒，整个排队过程不受通用 POST 的 210 秒限制。图片真正取得模型执行机会后才开始 195 秒预算。用户关闭编辑框、换图或离页时停止旧任务的前端观察；任务状态持久化，失效任务或断网会重读后端状态并提供恢复入口。日程未就绪的前 3 分钟每 5 秒查询，之后每分钟低频确认到终态；档案快速观察达到 4 分钟后也转为每分钟确认，确保排队及串行生成超过原观察窗口时仍自动显示结果。日程就绪后每分钟同步一次，离页停止所有观察。

遇到的原生行为已修复：range 输入在设置 min/max/step 前赋值会按默认步长取整，导致 0.5 显示成 1；现在配置范围后恢复保存值。切换板块归顶只在仍是当前目标页时执行，避免迟到请求打断其他页面。

## 插件更新、本地 ZIP、重载与卸载

本节只读核对了本地保存的两个官方 tag，没有对真实 AstrBot 执行安装、重载或卸载。源码根目录分别为 `.work/references/v4.26.0/AstrBot-4.26.0/` 与 `.work/references/v4.29.4/AstrBot-4.29.4/`（相对本工作区根目录）。

| 行为 | v4.26.0 | v4.29.4 |
| --- | --- | --- |
| 仓库或下载地址更新 | `astrbot/core/star/star_manager.py:1686` 的 `update_plugin` 调用 updater、处理依赖后 `reload` | 同文件 `:1863` 的 `update_plugin` 采用同一顺序 |
| 本地 ZIP 遇到已安装插件 | 同文件 `:1819` 的 `install_plugin_from_file`；`:1836–1838` 检测目标目录已存在并直接拒绝，不提供 ZIP 原位覆盖 | 同文件 `:2001` 进入 `_install_plugin_from_directory`；`:2075–2127` 校验同名 metadata，备份旧代码、终止旧实例、替换代码并加载；`:2131–2159` 尝试失败恢复旧代码 |
| 重载 | 同文件 `:842` 的 `reload` 终止和解绑旧实例后重新 `load`，未调用配置/数据清理 | 同文件 `:1018` 的 `reload` 采用同一模式 |
| 卸载资料清理 | 同文件 `:1511` 的 `uninstall_plugin` 接收 `delete_config/delete_data`，默认均为 `False`；`:1319–1355` 按勾选项删除配置文件与 `plugin_data/plugins_data` 目录 | 同文件 `:1678` 接收同样选项；`:1534–1589` 删除选中的配置、数据，并清理对应插件 KV 数据 |

两版 `dashboard/src/components/shared/UninstallConfirmDialog.vue:107–126` 的删除配置/数据选项默认未勾选，并把用户选择传到后端。因此，不把“卸载总会删除所有资料”作为兼容性结论；README 明确要求更新不要先卸载，并说明勾选清理后的真实丢失风险。用户已有日志中的 `Deleted the configuration file`、`Deleted persistent data` 对应 v4.29.4 清理函数中的删除分支。

更新器只替换 `data/plugins/<插件文件夹>/` 代码；插件的配置和 `StarTools` 数据位于外部目录。面向本地包，README 提供通用的“退出 AstrBot → 备份配置/数据 → 仅替换同名代码文件夹 → 启动检查”流程；已核实 v4.29.4 可直接使用「从文件安装」上传同名新版 ZIP。其他未逐个核对的小版本如果拒绝同名目录，采用通用流程，不建议先卸载绕过检查。

官方对应位置：[v4.26.0 插件管理器](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.0/astrbot/core/star/star_manager.py#L1819)、[v4.29.4 本地包替换](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/star_manager.py#L2001)、[v4.29.4 卸载清理](https://github.com/AstrBotDevs/AstrBot/blob/v4.29.4/astrbot/core/star/star_manager.py#L1534)。

## 第 4 轮：首次启动与模型队列

首次档案为空时，先等待本轮档案生成完成，再开始日程；档案本轮失败或执行超时后，日程继续，后续档案成功再只补齐日程未来部分，并刷新档案城市的天气。这一顺序取代第 3 轮的首次并行生成约定。

全部插件模型请求共用执行队列，空档案优先，聊天判断具有公平排队机会；等待队列不计入用途超时。模型执行预算集中为档案/日程各 180 秒、聊天判断 60 秒、小意外/长期事件各 120 秒、衣柜识图 195 秒。取消的生成不占自动次数；超时、连接失败按 2/10/30 分钟退避，内容失败保留原冷却间隔。具体自动上限与状态展示、并发和取消测试见 `审查报告_第4轮.md`。

超时按实际请求用途计算：当前 new_events/event_updates 随 schedule_prompt 同一 JSON 回复返回，使用 schedule 的 180 秒；集中策略中的独立 event 用途为 120 秒，当前未额外拆出独立请求。

第 5 轮新增 wardrobe/collect（POST，面板管理员写权限），用现有配置锁和日程修改锁提交收藏及引用；公开日程穿搭使用缩略图数据，不返回本地文件路径。阶段二内部 get_current_state 的穿搭元数据另包含 image_path，契约见 README。
