# elasticsearch-agent-observability

> 基于 **Elasticsearch + OpenTelemetry + Kibana**，为当前 agent 或指定 agent 自动构建采集、解析、存储、生命周期管理和报表入口的可观测能力。

## 这个项目真正要解决什么

这个项目不应该只是“帮你生成几份 YAML / JSON”。

它要解决的是更完整的一件事：

- 识别当前 agent / 某个指定 agent 的结构
- 生成适合这个 agent 的 OTel 采集入口
- 在 Elasticsearch 里准备好模板、pipeline、生命周期和写索引
- 给 Kibana 准备好可直接导入 / 应用的报表资产
- 让你能尽快把“采集 -> 入库 -> 查询 -> 看板”这条链路跑起来

所以它的产品定义应该是：
**用 Elasticsearch 原生能力把 agent observability 做起来**，而不是拿 Markdown 报表去假装一个平台。

## 核心使用场景

### 场景 1：给当前 agent 快速补齐可观测能力

比如你正在做一个 agent 项目，已经开始遇到这些问题：

- 到底跑了多少次
- 哪些 tool 老出错
- 哪些模型最贵
- latency 卡在哪
- retry / timeout 是不是在变多

你不想自己从零写：

- Collector 配置
- Elasticsearch 模板
- ingest pipeline
- ILM
- Kibana 报表入口

这时候这个 skill 的工作就是：
**自动给这个 agent 搭出一套能跑起来、也能继续扩的 observability 底座。**

### 场景 2：按用户需求，为某个指定 agent 构建观测面

比如用户说：

- “给这个 agent 接 Elasticsearch 可观测”
- “给订单分析 agent 做可观测和 Kibana 报表”
- “我想看它的 tool error、latency、token、cost”

那这个 skill 不应该停在“给你几份文件”。
它应该尽量把以下几个面都准备出来：

- 自动采集入口
- 自动解析 / 归一化入口
- 自动存储落点
- 自动生命周期策略
- 自动 Kibana 人类报表面

## 现在这版主路径是什么

```bash
python scripts/bootstrap_observability.py \
  --workspace /path/to/your-agent \
  --output-dir generated/bootstrap \
  --es-url http://localhost:9200 \
  --apply-es-assets \
  --kibana-url http://localhost:5601 \
  --apply-kibana-assets
```

这条命令会把几件事一口气串起来：

- 扫描 workspace，识别可监测模块
- 生成 OTel Collector 配置
- 生成 agent OTLP 环境模板和 Collector 启动脚本
- 生成 Elasticsearch 资产
- 把 ES 资产 apply 到目标集群
- 初始化首个写索引
- 生成 Kibana saved objects
- 可选把 Kibana 资产直接 apply 到 Kibana
- 额外生成一份 smoke 报表，方便先验证链路

## 产物不只是配置文件

```text
generated/bootstrap/
├── discovery.json
├── otel-collector.generated.yaml
├── run-collector.sh
├── agent-otel.env
├── report.md
├── elasticsearch/
│   ├── index-template.json
│   ├── ingest-pipeline.json
│   ├── ilm-policy.json
│   ├── report-config.json
│   ├── kibana-saved-objects.json
│   ├── kibana-saved-objects.ndjson
│   └── apply-summary.json
└── bootstrap-summary.md
```

这些文件各自的角色是：

- `discovery.json`：它觉得这个 agent 有哪些值得观测的模块
- `otel-collector.generated.yaml`：采集 / 转发入口
- `run-collector.sh`：Collector 启动脚本
- `agent-otel.env`：agent 运行时 OTLP 环境模板
- `index-template.json`：ES 存储结构
- `ingest-pipeline.json`：ES 入库解析 / 轻量清洗
- `ilm-policy.json`：生命周期管理
- `kibana-saved-objects.*`：Kibana 报表入口资产
- `apply-summary.json`：这轮到底有没有真正 apply 到 ES / Kibana
- `report.md`：一份 smoke 报表，用来先验证查询链路

## 报表面怎么理解

这里的主报表面应该是 **Kibana**。

`report.md` / JSON 不是产品主叙事，它只是一个很实用的 smoke / fallback：

- 当你刚 apply 完 ES 资产，想先确认查询有没有通
- 当你还没打开 Kibana，想先看一眼结果
- 当你要把结果交给自动化脚本继续处理

但真正给人看的长期入口，应该是：
**Kibana data view + saved search / dashboard 资产。**

## 这个项目最值的地方

它真正的价值不是“帮你写文件”，而是把这些原来容易只做一半的事补齐：

- 自动生成 OTel 接入面
- 自动准备 ES 入库面
- 自动准备 ILM
- 自动准备 Kibana 人类报表面
- 自动把 ES / Kibana 资产 apply 到目标环境

也就是说：
**不是停在 bootstrap 文档，而是尽量把 agent observability 的第一套能力真正落到 ES 原生栈里。**

## 当前实现重点

这版重点已经切到你关心的地方：

- ES 资产不只是生成，还支持 apply
- Kibana 资产不再缺席，已经进入主输出面和 apply 路径
- `report.md` 降级成 smoke / fallback，不再拿它冒充产品主报表面
- 不再拿别的 observability 产品当参照系，主叙事就是 **Elasticsearch + OTel + Kibana**

## 当前边界

这个项目现在的边界不是“只做一半”，而是：

- 默认只生成 OTel Collector 侧配置，不会自动改写你的 agent 代码
- 默认只做轻量归一化和脱敏，不会假装已经完成所有语义解析
- 依赖 Elasticsearch / Kibana 可访问
- Kibana 资产目前优先提供 data view + saved search 这类可直接落地的入口

这些边界的意思是：
**它站在 ES 原生栈这边，把该准备的资产都尽量准备好，但不会假装自己已经接管了你的 agent runtime 本体。**

## 仓库结构

```text
SKILL.md              agent 调用协议
scripts/              发现、生成、apply、报表主脚本
references/           配置说明、字段说明、报表说明
generated/            默认产出目录（不提交）
```

## 一句话总结

如果你要做的是“基于 Elasticsearch 的智能体可观测”，那主线就应该很清楚：

**OpenTelemetry 负责采集，Elasticsearch 负责存储和分析，Kibana 负责人类报表入口。**

这个项目的价值，就是帮你把这条线自动搭起来，而不是只吐几份文件然后停住。
