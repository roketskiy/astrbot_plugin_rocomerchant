# astrbot_plugin_rocomerchant

洛克远行商人推送是一个 AstrBot 插件，用于查询并推送「洛克王国：世界」远行商人的当前商品信息。

插件会从公开数据接口获取当前轮次、刷新时间、商人位置与商品列表，支持手动查询、会话订阅和定时推送。定时推送按会话记录发送状态，避免同一轮次重复打扰，同时在抓取失败、轮次未切换或部分会话发送失败时按配置重试。

## 功能特性

- 查询当前远行商人商品、限购数量、价格和分类。
- 支持在群聊或私聊会话中订阅定时推送。
- 支持自定义每日检查时间、重试间隔和最大重试次数。
- 支持推送消息前缀，便于多插件或多机器人场景区分来源。
- 手动查询失败时可回退最近一次成功缓存。
- 插件重载时会清理旧后台任务，避免重复调度。

## 兼容性

- AstrBot：`>= 4.9.2, < 5`
- Python：`3.12`
- 依赖：`httpx`
- 支持平台：以 [metadata.yaml](./metadata.yaml) 中的 `support_platforms` 为准。

## 安装

1. 将本仓库放入 AstrBot 插件目录：

   ```text
   AstrBot/data/plugins/astrbot_plugin_rocomerchant
   ```

2. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

   如果你的 AstrBot 环境会自动读取插件依赖，可跳过手动安装。

3. 在 AstrBot WebUI 中启用插件，并按需调整配置。

4. 重载插件或重启 AstrBot。

## 命令

| 命令 | 权限 | 说明 |
| --- | --- | --- |
| `/merchant now` | 普通用户 | 立即查询当前远行商人商品 |
| `/merchant subscribe` | 管理员 | 订阅当前会话的定时推送 |
| `/merchant unsubscribe` | 管理员 | 取消当前会话的定时推送 |
| `/merchant status` | 管理员 | 查看当前会话订阅状态与最近推送槽位 |

## 配置

配置项定义见 [_conf_schema.json](./_conf_schema.json)。

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable_push` | `bool` | `true` | 是否启用后台定时推送任务。关闭后仍可使用手动查询与订阅命令 |
| `push_times` | `list[string]` | `["08:10", "12:10", "16:10", "20:10"]` | 每日触发检查的北京时间列表，格式为 `HH:MM` |
| `retry_interval_minutes` | `int` | `3` | 抓取失败、轮次未匹配或推送未完全成功后的重试间隔，单位为分钟 |
| `max_retry_attempts` | `int` | `4` | 首次尝试后的最大补充重试次数 |
| `request_timeout_seconds` | `int` | `10` | 请求数据接口的超时时间，单位为秒 |
| `snapshot_fallback_for_now` | `bool` | `true` | 手动查询失败时是否使用最近一次成功缓存 |
| `message_header` | `string` | `""` | 推送消息前缀，非空时会作为第一行发送 |

## 定时推送规则

- 所有时间均按北京时间计算。
- 插件只在远行商人开放轮次内推送。当前轮次为 `08:00-12:00`、`12:00-16:00`、`16:00-20:00`、`20:00-24:00`。
- 到达 `push_times` 中任一检查时间后，插件会抓取数据并校验源站轮次是否已经切换到预期轮次。
- 若数据轮次尚未切换、请求失败或部分订阅会话发送失败，插件会按重试配置继续尝试。
- 同一轮次按订阅会话分别记录成功状态；已成功收到的会话不会重复推送，未成功的会话会继续参与重试。

## 数据与缓存

插件优先使用以下公开接口：

- `https://rocokingdomworld.org/api/merchant/live`

主接口失败时回退到：

- `https://rocokingdomworld.org/data/merchant.json`

成功抓取的数据会保存为最近一次快照，用于手动查询失败时回退展示。插件不会主动收集用户消息内容；订阅功能仅在 AstrBot KV 存储中保存会话来源标识和推送槽位状态。

## 本地验证

可以在不启动 AstrBot 的情况下验证数据源：

```bash
python fetch_merchant.py
```

命令会在终端输出当前抓取结果，并将标准化数据保存到：

```text
data/latest.json
```

## 故障排查

### 手动查询可用，但定时推送没收到

请先确认当前会话已执行 `/merchant subscribe`，并使用 `/merchant status` 检查订阅状态。随后查看 AstrBot 日志中是否出现 `rocomerchant` 相关记录，重点关注待推送订阅数、成功会话数和失败会话数。

### 日志提示轮次未匹配

这通常表示源站数据尚未刷新到当前北京时间对应轮次。插件会按 `retry_interval_minutes` 和 `max_retry_attempts` 自动重试。

### 日志提示未找到可用平台

这通常表示当前保存的会话来源无法由 AstrBot 路由到可发送平台。可在对应会话重新执行 `/merchant unsubscribe` 后再 `/merchant subscribe`。

### 插件重载后出现多次推送

当前版本会在初始化时清理同一事件循环中的旧后台任务。若仍出现重复推送，请确认没有在多个 AstrBot 实例或多个插件目录中同时加载本插件。

## 项目结构

```text
.
├── main.py              # AstrBot 插件入口、命令处理和定时推送调度
├── merchant_data.py     # 数据标准化、轮次判断和消息渲染
├── fetch_merchant.py    # 本地独立抓取验证脚本
├── _conf_schema.json    # AstrBot 配置项定义
├── metadata.yaml        # 插件元数据
└── requirements.txt     # Python 依赖
```

## 许可

本仓库尚未声明开源许可证。分发或二次发布前，请根据项目实际授权补充 `LICENSE` 文件。
