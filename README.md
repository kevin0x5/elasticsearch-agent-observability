# elasticsearch-agent-observability

> 给一个 agent 项目快速搭起 **可观测底座**。你可以把它理解成：先帮你看代码，再替你生成一套能跑起来的采集、入库和查询初稿。

## 你什么时候会想用它

通常是这种情况：

- 你手上已经有一个 agent 项目，但出了问题时很难知道卡在哪
- 你开始关心调用次数、耗时、错误、token、成本这些数据
- 你知道应该做可观测，但一想到 Collector、模板、索引、报表就头大

这个项目就是为这一步准备的。

它会先看你的项目结构，猜出哪些地方值得监测，再替你生成一套**能落地的初始配置**。

## 它真正帮你省掉什么

很多团队不是不想做 observability，而是第一步太碎：

- Collector 配置要写
- Elasticsearch 模板要建
- 生命周期要配
- 报表查询也要自己想

这个项目的价值不是“做完整平台”，而是把这些最容易卡住启动的事情先做成一个初稿。

你可以把它理解成：
**先帮你把底座搭起来，再决定后面怎么接。**

## 第一次使用，先看这件事有没有成立

第一次跑它，不要先追求“监控体系很完整”，先看一件事：

**它有没有基本看懂你的项目。**

如果它能识别出主要模块，并生成一套前后能对上的配置，那这一步就已经值回票价。

## 哪些情况适合直接用

- 你有一个 agent 或 skill 项目
- 你已经有 Elasticsearch 9.x，或者准备接 Elasticsearch 9.x
- 你想先得到一套能继续改的配置，而不是从零手写

## 哪些情况先别指望它

- 你想要一个现成的在线观测平台 UI
- 你希望它自动部署所有组件
- 你还没有 Elasticsearch，也不打算接 Elasticsearch

## 3 分钟上手

如果你不想先研究术语，先跑这一条命令。

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/your-agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200
```

你只需要替换两处：

- `/path/to/your-agent`：你的 agent 项目目录
- `http://localhost:9200`：你的 Elasticsearch 地址

这个命令**不会修改你的 agent 源码**，它只会在 `generated/bootstrap/` 下面生成文件。

## 跑完之后你会看到什么

```text
generated/bootstrap/
├── discovery.json
├── otel-collector.generated.yaml
├── elasticsearch/
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   └── report-config.json
└── bootstrap-summary.md
```

这些文件分别代表：

- `discovery.json`：它猜你的项目里有哪些关键模块
- `otel-collector.generated.yaml`：Collector 配置草稿
- `index-template.json`：字段模板
- `ingest-pipeline.json`：入库前做什么清洗
- `ilm-policy.json`：数据保留多久、什么时候滚动
- `report-config.json`：报表查询用什么索引和指标
- `bootstrap-summary.md`：给人看的结果摘要和告警

如果你只看一个文件，先看 `bootstrap-summary.md`。

## 第一次用，不必先搞懂所有术语

你现在不用先把 Collector、索引模板、ILM 这些词都搞明白。

第一次使用，你只要这样理解就够了：

- 它先看你的项目结构
- 再帮你产出一套“数据怎么采、进库前怎么整理、最后怎么查”的初稿
- 你确认方向大致对，再继续往下接真实环境

先把第一步跑通，比一开始把名词全背下来更重要。

## 它大概能识别什么

它会从项目里尝试识别这些常见部分：

- 入口脚本
- 命令行接口
- tool 调用层
- model 调用层
- memory / cache
- MCP 接口
- workflow / planner
- 已有 telemetry 相关代码

你不需要一开始就理解这些模块名。你只需要知道：
**它会根据项目结构，决定该生成哪些默认监测项。**

## 默认安全策略

这部分对新手也很重要：

- 如果你传了 `--es-user` / `--es-password`，默认也**不会**把凭据直接写进 YAML
- 默认会写成环境变量占位：`${env:ELASTICSEARCH_USERNAME}` / `${env:ELASTICSEARCH_PASSWORD}`
- 只有显式加 `--embed-es-credentials`，才会把凭据内嵌进生成文件
- 默认会对 `gen_ai.prompt`、tool 参数、tool 结果做删除，避免把敏感内容原样落盘

## 一个关键约定

当前默认契约下，logs 和 traces 都会写入同一个 alias：`<index-prefix>-events`。

你可以先把它理解成：
**所有生成的配置，默认都指向同一个统一入口。**

这样做的好处是：Collector、index template、ILM、report query 四处更容易保持一致，不容易出现“写进去了，但报表查不到”的情况。

## 跑完后怎么判断有没有问题

优先看 `bootstrap-summary.md` 里的提示。

常见情况：

- 出现 `Discovery reached the --max-files limit`：项目太大，可能没扫全
- 出现 `No monitorable modules were detected`：路径不对，或者当前启发式没有识别出来
- 出现 “credentials were not written to disk”：这不是报错，是在提醒你当前是更安全的默认模式
- 如果你显式使用了 `--embed-es-credentials`：把生成的 YAML 当成敏感文件处理

## v1 能力边界

当前版本解决的是 **bootstrap**，不是完整 observability 平台。

它会做：

- 扫描项目结构
- 生成 Collector 配置
- 生成 Elasticsearch 资产
- 生成报表配置

它现在**不会**做：

- 自动部署 Collector
- 自动接管历史数据
- 提供完整在线 trace UI
- 替代 Langfuse / Phoenix 这类平台的工作台

## 目标环境

| 环境 | 状态 |
|------|------|
| 自建 Elasticsearch 9.x | ✅ 支持 |
| 腾讯云 Elasticsearch Service 9.x | ✅ 支持 |
| 其他托管环境 | 🔜 后续扩展 |

## 仓库结构

```text
SKILL.md              agent 行为协议
scripts/              发现、生成、报表脚本
references/           配置说明、字段说明、报表说明
assets/               默认模板
generated/            产出目录（默认不提交）
```

## 给零基础读者的建议

第一次不要试图“把整套监控一次性上完”。

先做这三件事：

1. 跑 `bootstrap_observability.py`
2. 打开 `bootstrap-summary.md`
3. 看 `discovery.json` 里识别出来的模块是不是大致靠谱

先确认“它有没有看懂你的项目”，再去接 Collector 和 Elasticsearch，会轻松很多。
