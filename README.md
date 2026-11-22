# Generic Shop Monitor

面向自托管的只读商品目录监控器。Python 标准库轮询已配置的公开目录，识别新上架、重新上架、补货和库存进入最后五件的事件；状态、待发提醒和可选日报保存在本地。支持配置标准商品目录接口，另有可选的第三方货架适配器。项目没有 Web UI 或 dashboard，不监听/发布任何端口。

## 功能与默认安全行为

- 新上架/补货/最后五件提醒按配置的 OneBot 目标发送；所有 OneBot 目标及提及名单均为显式可选配置，空白即禁用。OneBot 默认仅连接 loopback，或同一私有 Docker 网络中的 `onebot` 服务别名；不要把 OneBot 端口发布到公网。
- **低价新品提醒只走 Telegram 私聊**，不会回退到 OneBot/QQ。`new_listing_price_threshold_cny` 默认 `20`，采用严格小于：`19.99` 符合，`20.00` 不符合。`telegram_chat_id` 在样板中留空，未配置时不发送且不会改投其他渠道。
- Telegram bot token 只从 `TELEGRAM_BOT_TOKEN` 环境变量读取，绝不写入 JSON、日志或 Git。通过 `.env` 传入容器，并限制该文件权限。
- 首轮采集建立基线，不推送旧目录。目录失败、未知库存不会误判为下架/有货；提醒按事件去重并持久化投递回执。
- DeepSeek 分类为可选项。Docker 默认使用 `--no-ai`，监控无需 AI 凭据。Node.js 也不包含在标准镜像中，只在可选 Excel/PNG 导出时需要。
- 容器以非 root 用户运行；根文件系统只读，只有持久卷 `/data` 可写，临时目录为受限 tmpfs；启用 `no-new-privileges`、丢弃 capabilities、健康检查和 `unless-stopped` 重启策略。compose 不发布端口。

## 配置

```sh
cp config.example.json config.json
```

先在 `config.json` 中填写至少一个你有权监控的店铺。通用目录适配器需要公开的店铺 token、店铺名称和该平台的 HTTPS 源站，例如：

```json
{
  "id": "store-a",
  "name": "Store A",
  "token": "REPLACE_WITH_PUBLIC_CATALOG_TOKEN",
  "base_url": "https://shop.example.com"
}
```

`base_url` 只接受 HTTPS 公共源站（不带路径、查询、端口或认证信息）。适配器会请求该站点的 `/shopApi/Shop/goodsList`，只读取卡密商品目录；如果你的平台 API 不兼容，需要为它实现/选择对应 source adapter。例子中的 `example.com` 和 token 是占位符，不能直接用于采集。`shops` 为空的样板故意不能启动，启动器会拒绝空店铺配置。

- 默认轮询间隔为 20 秒；最小允许值为 15 秒。
- 阈值字段为 `new_listing_price_threshold_cny`，默认值 `20`，小于比较为严格 `<`。
- Telegram 私聊：创建 bot，并在你自己的私聊中启动 bot；把 chat ID 写进本地 `config.json` 的 `telegram_chat_id`。保持仓库样板中的 chat ID 为空，不要提交个人 chat ID。
- 在 `.env` 中设置 `TELEGRAM_BOT_TOKEN`。可从 `.env.example` 复制模板；`.env` 已被 Git 和 Docker build context 排除。主机上建议 `chmod 600 .env`，仅让部署用户可读，并避免把它打印到终端、CI 日志或工单中。
- 可选 OneBot：仅在你确实使用 OneBot 时，设置 `onebot_priority_group_id`、`onebot_all_group_id`、`onebot_private_user_id` 中需要的字段；使用自己的目标 ID。可选提及名单为 `onebot_priority_mentions`。未配置的目标不发送，低价新品仍只走 Telegram。Docker 中如需 OneBot，令容器与 OneBot 服务处于私有网络并将 `onebot_url` 指向 `http://onebot:3002`；不要添加公网 `ports:` 映射。
- 可选 DeepSeek 分类：需将容器启动参数中的 `--no-ai` 移除，并通过 `DEEPSEEK_API_KEY` 环境变量提供密钥；不要把密钥放进 `config.json`。AI 失败不会阻断规则明确的事件。
- `service_status.enabled` 和 `analytics.enabled` 默认关闭。开启 analytics 后，数据库、报告和状态都落在持久数据目录。

## VPS 上使用 Docker Compose

要求 Docker Engine 和 Docker Compose v2。仓库目录中创建本地运行配置与环境文件：

```sh
cp config.example.json config.json
cp .env.example .env
chmod 600 .env
$EDITOR config.json
$EDITOR .env
```

填好至少一个真实、有效且已获准访问的店铺后，再启动。把 Telegram token 填入 `.env` 的 `TELEGRAM_BOT_TOKEN`。不要在 `compose.yaml` 中写 token，也不要把运行时 `.env` 或 `config.json` 提交到 Git。

```sh
docker compose build
docker compose run --rm --no-deps monitor --help
docker compose up -d
docker compose ps
docker compose logs --tail=100 monitor
```

没有配置店铺时 `up` 会安全失败，不会开始轮询。服务运行后会访问配置的商品源；只有配置了 chat ID 和 token 后，低价新品才会向 Telegram 发送。健康检查会检查最近一次完整轮询和进程状态。数据保存在命名卷 `monitor-data` 的 `/data` 下（包括 `state/monitor.json`、日志、可选数据库及报告），容器替换/重启时保留。`docker compose down` 保留卷；`docker compose down -v` 会删除该卷中的所有状态与报告，应视为破坏性操作。备份前停止容器，按 Docker 卷方式备份数据。

Compose 不发布任何入站端口，因为本项目没有 UI。VPS 防火墙仍应默认拒绝不需要的入站连接。若启用 OneBot，请仅把它置于受控私有网络；监控容器需要访问已配置的商品源和 Telegram HTTPS API。

### 编译、离线测试与镜像冒烟

在开发机运行：

```sh
python3 -X utf8 -m unittest discover -s tests -v
python3 -m compileall -q .
git diff --check
```

Docker 冒烟命令 `docker compose run --rm --no-deps monitor --help` 只启动 CLI 帮助，不会开始采集、访问 Telegram 或连接 OneBot。`docker compose build` 只构建应用镜像，不运行监控逻辑（Docker 可能需要下载基础镜像和 Alpine tzdata）。实际 `docker compose up -d` 才会开始请求配置的商品源。

### 可选 Excel/PNG 导出

基础镜像有意不安装 Node.js：轮询、通知、HTML/CSV/SVG/JSON 日报不依赖 Node。Excel 和 PNG 导出需要 Node.js 与 `@oai/artifact-tool`；如需启用，建议从本镜像构建仅供自己使用的派生镜像安装 Node 与包，或使用共享数据卷的独立导出服务，并配置 analytics 中的 Node 可执行文件和 artifact-tool 模块路径。不要为了基础监控而安装 Node，也不要在公开样板里加入本地包路径或凭据。

## 离线样本与许可说明

`tests/fixtures/catalog/` 中的数据使用合成店铺/商品标识与合成标题，仅用于离线行为测试。测试使用注入的传输层，不会请求真实商品源、Telegram、OneBot、AI 服务或其他外部网络。仓库不包含个人运行配置、秘密、持久状态或部署回执。项目目前未附加开源许可证；第三方名称、协议和商标仍归各自权利人所有。
