# IBKR Flex Query setup

You only need to do this once; it takes about 10 minutes. Labels below follow the English IBKR web portal (Client Portal).

## 1. Create an Activity Flex Query

Log in to the IBKR web portal → **Performance & Reports → Flex Queries**, then click **+** next to **Activity Flex Query**.

Give it any name, for example `Daily_Performance`.

### Sections

Except for Account Information, open each section and choose **Select All** fields.

| Section | Required | Notes |
|---|---|---|
| Account Information | ✅ | **Don't select all.** Only tick Account ID, Currency, Account Type, Date Opened and Date Funded, so your name and address never end up in the data files |
| Net Asset Value (NAV) in Base | ✅ | In the options, **do not** exclude the prior report date (it provides the starting NAV). Excluding the long/short breakdown is optional |
| Change in NAV | ✅ | Keep the **Mark-to-Market** option |
| Cash Transactions | ✅ | Tick every transaction type, choose **Detail** (not Summary) |
| Trades | ✅ | Level of detail: **Executions** |
| Open Positions | ✅ | Level of detail: **Summary** |
| Mark-to-Market Performance Summary in Base | ✅ | Daily P&L and positions per symbol; the exposure and leverage charts depend on it |
| Realized and Unrealized Performance Summary in Base | Recommended | |
| Cash Report | Recommended | |
| Corporate Actions | Recommended | Needed for splits and reverse splits |
| Transfers | Recommended | Needed if you transferred positions in |
| Option Exercises, Assignments and Expirations | Optional | If you trade options |
| FX P/L Details | Optional | If you hold several currencies |

Everything else (HK IPO, IBG notes, securities lending, debit card, FDIC, and so on) can be left out.

> Note: **Trade Transfers** is not the same as Cash Transactions and is not needed.

### Delivery Configuration

| Setting | Value |
|---|---|
| Format | **XML** |
| Period | **Last 365 Calendar Days** |

### General Configuration

| Setting | Value |
|---|---|
| Date Format | `yyyyMMdd` |
| Time Format | `HHmmss` (default) |
| Date/Time Separator | `;` (default) |
| Profit and Loss | Default |
| Include Currency Rates? | **Yes** |
| **Breakout by Day?** | **Yes** (most important: without it you only get the starting and ending NAV, so there is no daily curve) |
| Other switches | No |

Save the query. In the **Activity Flex Query** list, click the **ⓘ** icon next to its name to see the **Query ID** (a number).

## 2. Enable the Flex Web Service and generate a token

On the same page, click the gear icon of **Flex Web Service Configuration**:

1. Tick **Flex Web Service Status** to enable it
2. Click **Generate New Token**. The page shows the **current token** and its validity period
3. Optional: restrict access to your own IP address

Write down two things: the **Query ID** and the **token**. The token works like a read-only password: **don't share it, don't post screenshots of it, and never commit it to Git**.

Put the token's expiry date into `token_expiry` in `analysis/settings.json` (for example `"2027-06-30"`), and the dashboard will remind you 30 days before it expires.

## 3. History older than a year

The Flex Web Service returns at most 365 days per request. If your account is older than a year, backfill the earlier history once:

1. In the **Activity Flex Query** list, click the **→** (run) icon next to your query
2. Choose a **custom date range**, run it one year at a time, format XML
3. Put the downloaded XML files into `data/history/`

Daily pulls are merged and de-duplicated with these files automatically. `data/raw/` also keeps one pull per month indefinitely, so history is not lost after the first year.

## Common error codes

| Code | Meaning | What to do |
|---|---|---|
| 1012 / 1015 | Token expired or invalid | Generate a new token, then run `reset_token.bat` |
| 1019 | Report is being generated | The script waits and retries automatically |
| 1018 | Too many requests | Wait a few minutes and run it again |
| 1009 / 1021 | Server busy / temporarily unavailable | Try again later |
