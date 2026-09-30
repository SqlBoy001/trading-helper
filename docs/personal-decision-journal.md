# 个人股票决策记录（本地原型）

把“当时怎么想、依据哪版计划、助手提醒了什么、最终做了什么”保留下来，供后续分析和复盘。这个版本提供持久账本、Agent工具和无需模型的命令行；另有[自用截图网页](personal-web.md)，支持本地文字识别与确认录入。尚无自动行情核验、行为诊断或交易执行。

## 可以直接使用的入口

在项目Python环境中，从仓库根目录运行：

```sh
cd agent
python -m src.decision_journal --help
python -m src.decision_journal append /absolute/path/event.json
python -m src.decision_journal history personal:600519.SH
python -m src.decision_journal get dj_RETURNED_ID
```

数据写到 `VIBE_TRADING_HOME/decision_journal.sqlite3`，未设置时为 `~/.vibe-trading/decision_journal.sqlite3`。可先设置独立测试目录，避免把试验混入个人记录。命令不调用模型、不联网、不发送订单。

项目Agent重建工具注册表后，自动发现 `record_decision_event` 和 `read_decision_journal`。在运行已有模型的CLI/Web会话中，可说“把下面内容记录为交易意图，还没有成交”，或者“完整读取这条计划及更正，再帮我核对”。工具注册不是助手一定会调用的保证；关键结果应核对返回的记录ID。当前没有新增MCP导出，也没有开启旧定时任务。

## 第一笔从意图开始

`event.json` 示例不包含个人仓位上限，也不宣称已经成交：

```json
{
  "journal_id": "personal:600519.SH",
  "kind": "intent",
  "request_key": "my-first-intent-001",
  "source": "user",
  "source_ref": "用户原话：想先观察，尚未决定是否买入。",
  "data": {
    "action": "watch",
    "reason": "先核对公开资料和自己的买入理由"
  }
}
```

返回 `event.id` 后可用get完整读取。相同请求重试必须复用相同request_key及原始参数；重复不会多记一条。改变内容需新key，不能拿旧key改历史。记录包含服务器recorded_at和发生时间occurred_at；迟录成交可以填带时区的过去发生时间，但不能倒签计划。

## 写入合同

每条共同字段：journal_id、kind、request_key、source（user/assistant/import）、source_ref、data。source_ref保存原话或明确来源定位，不放密码或token。journal_id是稳定分组，可以包含账户别名及股票代码，不是经系统验证的证券身份。

| kind | data必填 | parent_id |
|---|---|---|
| plan | thesis、rules字符串数组、confirmed布尔值；若true还要confirmation_quote | 首版不填，修订必须指向最新计划版本 |
| intent | action、reason | 可省略，由代码关联发生时已记录、已确认且生效的计划；显式指定时必须匹配 |
| reminder | status、observation、next_step、evidence_ids数组 | 对应intent |
| decision | action、reason | 对应intent |
| execution | symbol、side、quantity、price、currency；import来源还需external_source/external_id | 可选intent，未记录时不虚构 |
| snapshot | positions（symbol/quantity列表）、cash（数字或null）、complete（布尔）、notes | 无父记录 |
| review | assessment | 对应intent |
| correction | reason、replacement完整替换data | 原事件；已有更正时必须指向最新更正 |

intent/decision的action支持buy/add/reduce/sell/watch/defer/cancel；execution的side仅buy/sell。reminder状态支持plan_conflict/plan_consistent/needs_information/thesis_review，**由调用者给出，存储层不验证金融判断是否正确**。建议额外保存hypothesis、alternatives、引用原文URL及数据时间；通用附加字段可以保留，但未经领域校验，不作为已核实事实。

execution是成交记录，不是下单命令；它不会更新项目Portfolio中的真实余额或持仓。导入时external_source要稳定标识券商和账户别名，external_id标识单笔成交，重导同一笔会被拒绝，不能把一个订单的多笔成交误合成一笔。手工录入的语义重复无法完全检测，需调用者正确复用request_key并人工核对。

## 计划版本和更正

- 新计划默认从现在生效，可设未来effective_at，不能早于记录时间；计划不接受occurred_at倒签。历史手写计划可以作为待核实资料另存，但不能伪装成早已存在于本系统的规则。
- confirmed=false是草案，**不会撤销既有已确认计划**；需要替换时写完整新规则并记录用户确认。系统不内置默认仓位、止损比例，也不验证计划本身是否合理。
- 来源标成assistant时，不能确认计划、写用户实际决定/成交，或更正这些实际行动。但source/confirmation_quote是调用者归因，**不是独立的身份验证或人类审批系统**；仍应核对用户原话。
- 通用correction不能修改计划；修订计划必须新建plan版本。成交更正可以纠正数量/价格，但不能更换外部成交身份。
- get返回原始事件及更正链，不会用新值覆盖原文；使用数据时需要检查最新更正。history的as_of按记录可知时间筛选；不会把今天补录的昨天交易塞进“昨天已知”视图。
- 每条记录最多128KiB，超出明确拒绝，不静默截断。history分页返回完整记录，next_after非空时继续读取。精确get不走remember的2000字摘要。

SQLite事务处理幂等与并发，表有禁止UPDATE/DELETE的触发器；这是正常接口的追加约束，不是防管理员改库的密码学审计。备份需遵循SQLite一致性备份方式。无自动清理/覆盖/持仓同步，也无自动交易、消息投递或外部模型调用。

## 验证与运行限制

测试：`PYTHONPATH=agent python -m unittest discover -s agent/tests -p test_decision_journal.py -v`。测试使用临时目录和合成数据，无需pytest或网络，但工具层依赖项目既有Python环境。

隔离Docker实例若使用 `/tmp` 作为运行根，容器重启会清空该位置，不能作为长期个人账本。正式使用须在已有持久运行根中运行；目前原生产实例未被重启或迁移。本次验证样例保存在单独的主机目录并通过CLI/工具测试，不含真实账户。
