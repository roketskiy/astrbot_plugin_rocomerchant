# astrbot_plugin_rocomerchant

AstrBot 插件：抓取 `rocokingdomworld.org` 的远行商人当前商品数据，支持手动查询与定时推送。

## 功能

- `/merchant now`
  - 手动查询当前轮次商品
- `/merchant subscribe`
  - 管理员订阅当前会话的定时推送
- `/merchant unsubscribe`
  - 管理员取消当前会话的定时推送
- `/merchant status`
  - 管理员查看当前会话订阅状态

定时推送规则：

- 北京时间 `08:10 / 12:10 / 16:10 / 20:10` 开始尝试
- 若当前源站数据轮次未切换成功，则按配置的重试间隔继续重试
- 只要同一轮次已经成功推送过，就不会重复推送

## 目录

- [main.py](/E:/work/rocomarchant/main.py)
  - AstrBot 插件入口
- [merchant_data.py](/E:/work/rocomarchant/merchant_data.py)
  - 公共抓取、标准化、轮次判断、消息文本渲染
- [fetch_merchant.py](/E:/work/rocomarchant/fetch_merchant.py)
  - 独立验证脚本，不依赖 AstrBot
- [_conf_schema.json](/E:/work/rocomarchant/_conf_schema.json)
  - 插件配置定义
- [metadata.yaml](/E:/work/rocomarchant/metadata.yaml)
  - 插件元数据

## 环境要求

- AstrBot `>= 4.9.2, < 5`
- Python 3.12 可用
- 插件依赖：
  - `httpx`

## 安装

将当前仓库目录作为插件目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_rocomerchant
```

然后在该目录安装依赖，或由 AstrBot 自动读取 [requirements.txt](/E:/work/rocomarchant/requirements.txt)。

## 配置

在 AstrBot WebUI 中配置：

- `enable_push`
- `retry_interval_minutes`
- `max_retry_attempts`
- `request_timeout_seconds`
- `snapshot_fallback_for_now`
- `message_header`

## 数据来源

优先使用：

- `https://rocokingdomworld.org/api/merchant/live`

失败后回退：

- `https://rocokingdomworld.org/data/merchant.json`

## 本地独立验证

可以先不启动 AstrBot，直接运行：

```bash
python fetch_merchant.py
```

运行后会：

- 在终端打印当前抓取结果
- 把标准化 JSON 保存到 `data/latest.json`

## 当前已验证项

- 主接口抓取成功
- 备用接口可访问
- 轮次校验逻辑可运行
- 抓取结果可保存到本地 `latest.json`
- 插件目录结构、配置文件和 KV 存储接口已接入 AstrBot
- 插件已在实际 AstrBot 环境中完成加载验证
