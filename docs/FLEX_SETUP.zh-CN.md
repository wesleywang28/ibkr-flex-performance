# IBKR Flex Query 设置指南

[English](FLEX_SETUP.md) | **简体中文**

只需要设置一次，大约 10 分钟。下面括号里是 IBKR 英文网页版（Client Portal）的原始名称；中文界面的翻译可能略有不同，对照英文名称找即可。

## 1. 创建 Activity Flex Query

登录 IBKR 网页版 → **业绩与报告（Performance & Reports）→ Flex 查询（Flex Queries）**，点 **活动 Flex 查询（Activity Flex Query）** 旁边的 **+**。

名字随意，比如 `Daily_Performance`。

### 要勾选的部分（Sections）

除了"账户信息"，其余每个部分打开后都选 **全选（Select All）**。

| 部分 | 是否必需 | 说明 |
|---|---|---|
| 账户信息（Account Information） | ✅ | **不要全选。** 只勾 Account ID、Currency、Account Type、Date Opened、Date Funded，这样姓名和地址不会进入数据文件 |
| 基础货币资产净值（Net Asset Value (NAV) in Base） | ✅ | 选项里**不要**排除前一报告日（它提供期初净值）。是否排除多空明细都可以 |
| 资产净值变动（Change in NAV） | ✅ | 保留 **Mark-to-Market** 选项 |
| 现金交易（Cash Transactions） | ✅ | 勾选所有交易类型，选 **明细（Detail）**，不要选汇总（Summary） |
| 交易（Trades） | ✅ | 明细级别选 **执行（Executions）** |
| 未平仓头寸（Open Positions） | ✅ | 明细级别选 **汇总（Summary）** |
| 基础货币按市值计价业绩汇总（Mark-to-Market Performance Summary in Base） | ✅ | 每个标的的每日盈亏和持仓；敞口和杠杆图依赖它 |
| 基础货币已实现和未实现业绩汇总（Realized and Unrealized Performance Summary in Base） | 建议 | |
| 现金报告（Cash Report） | 建议 | |
| 公司行动（Corporate Actions） | 建议 | 拆股、合股时需要 |
| 转账（Transfers） | 建议 | 有持仓转入时需要 |
| 期权行权、指派和到期（Option Exercises, Assignments and Expirations） | 可选 | 交易期权时勾选 |
| 外汇盈亏明细（FX P/L Details） | 可选 | 持有多种货币时勾选 |

其余部分（港股 IPO、IBG 票据、证券借贷、借记卡、FDIC 等）都可以不选。

> 注意：**交易转移（Trade Transfers）** 和现金交易（Cash Transactions）不是一回事，不需要勾选。

### 交付配置（Delivery Configuration）

| 设置 | 值 |
|---|---|
| 格式（Format） | **XML** |
| 期间（Period） | **过去 365 个日历日（Last 365 Calendar Days）** |

### 常规配置（General Configuration）

| 设置 | 值 |
|---|---|
| 日期格式（Date Format） | `yyyyMMdd` |
| 时间格式（Time Format） | `HHmmss`（默认） |
| 日期/时间分隔符（Date/Time Separator） | `;`（默认） |
| 盈亏（Profit and Loss） | 默认 |
| 包含汇率？（Include Currency Rates?） | **是（Yes）** |
| **按天细分？（Breakout by Day?）** | **是（Yes）**（最重要：不开的话只有期初和期末净值，画不出每日曲线） |
| 其他开关 | 否（No） |

保存。在 **活动 Flex 查询** 列表里，点查询名称旁边的 **ⓘ** 图标，可以看到 **Query ID**（一串数字）。

## 2. 开启 Flex Web Service 并生成 Token

在同一页面，点 **Flex Web Service 配置（Flex Web Service Configuration）** 的齿轮图标：

1. 勾选 **Flex Web Service 状态（Flex Web Service Status）** 开启服务
2. 点 **生成新令牌（Generate New Token）**，页面会显示 **当前令牌（current token）** 和有效期
3. 可选：限制只允许你自己的 IP 地址访问

记下两样东西：**Query ID** 和 **Token**。Token 相当于一个只读密码：**不要分享、不要截图发给别人，也绝对不要提交到 Git**。重新生成 Token 会让旧的立即失效。

把 Token 的到期日填到 `analysis/settings.json` 的 `token_expiry`（例如 `"2027-06-30"`），仪表盘会在到期前 30 天提醒你。

## 3. 一年以前的历史数据

Flex Web Service 每次最多返回 365 天。如果账户开户超过一年，需要一次性补齐更早的历史：

1. 在 **活动 Flex 查询** 列表里，点查询旁边的 **→**（运行）图标
2. 选择 **自定义日期范围**，每次跑一年，格式选 XML
3. 把下载的 XML 文件放进 `data/history/`

每天拉取的数据会自动和这些文件合并去重。`data/raw/` 也会每个月永久保留一份，所以满一年后历史数据不会丢。

## 常见错误代码

| 代码 | 含义 | 处理方法 |
|---|---|---|
| 1012 / 1015 | Token 过期或无效 | 生成新 Token，然后运行 `reset_token.bat` |
| 1019 | 报表正在生成 | 脚本会自动等待并重试 |
| 1018 | 请求太频繁 | 等几分钟再运行 |
| 1009 / 1021 | 服务器繁忙 / 暂时不可用 | 稍后再试 |
