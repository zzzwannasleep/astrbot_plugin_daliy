# astrbot_plugin_daliy

AstrBot 的 Telegram 每日晨报插件。

这个版本不走 QQ 平台那种高度定制模板，而是按 AstrBot 通用插件方式实现：

- 定时主动推送到已订阅会话
- 每个会话可单独设置天气城市
- 晨报内容由天气、RSS 新闻、每日一句、可选诗词组成
- 所有订阅状态使用 AstrBot 插件 KV 存储

## 安装

将本插件放到 AstrBot 的 `data/plugins/astrbot_plugin_daliy/` 目录下，然后在 WebUI 中安装或重载插件。

如果你的 AstrBot 是手动部署，插件目录结构应类似：

```text
AstrBot/
  data/
    plugins/
      astrbot_plugin_daliy/
        main.py
        metadata.yaml
        _conf_schema.json
        requirements.txt
```

## 配置项

插件配置在 WebUI 中可直接编辑，重点项：

- `delivery_time`: 每日发送时间，默认 `08:00`
- `delivery_timezone`: 发送时区，默认 `Asia/Shanghai`
- `default_city`: 默认天气城市，默认 `北京`
- `image_mode_enabled`: 是否启用图片模式，默认 `false`
- `auto_delete_command_on_telegram`: Telegram 下是否自动删除你发出的命令消息，默认 `false`
- `rss_urls`: RSS 源列表，每行一个 URL
- `news_limit`: 晨报中展示的新闻数量
- `http_proxy`: 如果 Telegram / RSS / 天气接口需要代理，可以在这里设置

## 指令

- `/daily subscribe`
- `/daily subscribe shanghai`
- `/daily unsubscribe`
- `/daily city shenzhen`
- `/daily preview`
- `/daily preview hangzhou`
- `/daily news`
- `/daily status`
- `/daily sendnow`

如果你想用不带空格的 Telegram 风格命令，也支持：

- `/dailysubscribe`
- `/dailysubscribe shanghai`
- `/dailyunsubscribe`
- `/dailycity shenzhen`
- `/dailypreview`
- `/dailypreview hangzhou`
- `/dailynews`
- `/dailystatus`
- `/dailysendnow`

兼容命令同样可用：

- `/morning subscribe`
- `/morning preview`
- `/morning news`
- `/晨报 订阅`
- `/晨报 预览`

说明：

- `subscribe` 会把当前会话的 `unified_msg_origin` 记录下来，之后定时主动推送到这个会话
- `sendnow` 需要 AstrBot 管理员权限
- `preview` 只预览当前会话的一份晨报，不会影响订阅列表
- `dailynews` 只拉取当前 RSS 新闻速览，不会改动订阅状态
- 开启 `auto_delete_command_on_telegram` 后，Telegram 会先尝试删除你触发命令的那条消息，再返回结果；删除失败不会影响晨报发送

## 内容来源

- 天气：Open-Meteo
- 每日一句：Hitokoto
- 诗词：今日诗词（可选）
- 新闻：用户配置的 RSS 源

如果某个外部接口失败，插件会跳过该部分内容，不会让整条晨报直接崩掉。
如果开启图片模式，插件会调用 AstrBot 的 `text_to_image()` 把文本晨报渲染成图片；如果渲染失败，会自动退回文本发送。

## 已知限制

- 这是 Telegram 定向场景插件，`metadata.yaml` 里只声明了 `telegram`
- 默认 RSS 使用中新网滚动新闻源；如果不合适，可以在配置里替换
- 图片模式目前是文转图，不是定制海报卡片渲染
