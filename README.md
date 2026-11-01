# Polymarket BTC 5 分钟 Up/Down 本地模拟器

程序通过 Polymarket 的公开 Gamma 接口查找当前 BTC 5 分钟市场，再通过公开 CLOB WebSocket 读取 Up/Down 盘口。它只在本机模拟成交和盈亏：不需要 API key 或钱包，也不会发送真实订单。

## 默认策略

- 初始虚拟现金：$100；每次最多投入：$4。
- 每个 5 分钟市场开场后等待 20 秒，再开始判断买入信号；开场等待期间的价格变化不会被当作入场确认。
- **入场严格过滤**：
  - 价格从 58¢ 下方上穿 58¢ 后，必须等待 **2 次连续报价上行**确认（`--confirm-up-ticks 2`），防止单 tick 假突破。
  - **点差保护**：买卖点差必须 `<= 0.04`（`--max-spread 0.04`），防止因过宽点差一进场即产生巨额账面亏损。
  - **盘口深度保护**：卖盘可用股数必须满足最少 5 股（`--min-depth-shares 5.0`），且卖价在 60¢–63¢ 之间。
- **出场保护机制**：
  - **突破停滞 / 失败撤退**：开仓持仓满 60 秒后（`--stall-timeout-seconds 60`），若价格仍未能站上入场均价（`mid <= entry_price`），判定为突破失败（`breakout_failure`）主动平仓。硬止损和临近结算强制退出规则仍独立生效。
  - **放宽硬止损**：默认硬止损放宽至入场价的 0.85 倍（`--stop-multiple 0.85`，如 0.60 买入止损在 0.51），防止被正常的盘口瞬间波动误杀。
  - **止盈**：为模拟入场均价的 1.2 倍（`--take-profit-multiple 1.20`）。
- 到本轮结束前 15 秒仍未触发止盈/止损时，尝试按公开买盘（bids）平仓；盘口不足时按实际可成交数量记录部分成交。
- 模拟成交计入可配置的 taker fee（默认 7% 的费率参数，按 Polymarket 加密市场公式估算）。实际费用可能因市场设置变化；模拟结果仅供比较策略。
- 只同时持有一个仓位，每个 5 分钟市场最多入场一次。

## 高确定性价格策略（优化分支 feature/high-confidence-optimized）

通过独立策略分支模拟买入市场价格显示高胜率的 Up/Down 结果份额，针对历史交易日志中暴露的**止损恶性滑点穿透**与**秒级虚假波动杀跌**进行了全面优化：

- 启动参数使用 `--strategy high-confidence`。
- **入场点差与确认**：
  - 卖一价（ask）进入 78¢–95¢ 区间后，需每 0.5 秒采样到 2 次连续严格上涨（共 3 个递增 mid 样本）才入场；样本间隔超过 1.5 秒或价格回落时重新确认。
  - **更紧凑的点差限制**：高确定性模式默认要求点差 `<= 0.025`（`--high-confidence-max-spread 0.025`），防止过宽点差导致入场即濒临止损。
  - **买盘承接深度保护**：入场时严格检查止损底线价以上的买盘总深度必须满足至少 5 股（`--high-confidence-min-bid-depth 5.0`），无承接盘或承接不足时不盲目开仓。
- **出场与止盈止损优化**：
  - **目标止盈**：默认为入场均价的 1.2 倍，最高封顶 99¢。
  - **基准止损**：按实际模拟成交均价计算：`均价 × (1 - 0.097)`。
  - **止损防插针毛刺（Anti-Whipsaw）**：当 bid 跌破止损价时，需同时满足中间价 `mid <= stop + 0.015`（`--high-confidence-stop-mid-buffer 0.015`），过滤瞬时虚假点差拉大导致的假突破被止损。
  - **恶性滑点硬封顶（Slippage Floor）**：止损单平仓时引入最低限价保护，单笔卖出成交价不允许低于 `stop - 0.035`（`--high-confidence-max-stop-slippage 0.035`），严禁市价扫穿到 0.54 等稀薄流动性深渊。
  - **保本与动态移动止损（Breakeven & Trailing Stop）**：
    - 当价格上涨 `mid >= entry_price + 0.06` 时（`--high-confidence-breakeven-trigger 0.06`），自动将止损位提升至保本位（`entry_price + 0.005`，覆盖手续费）。
    - 启动动态移动止损跟踪（`--high-confidence-trailing-stop 0.07`），锁定浮动盈利，避免大幅盈利回撤转亏。
- 此模式关闭默认的停滞退出；到期前强制退出和其他模拟成交规则仍然有效。
- 可配参数：`--high-confidence-min-price`、`--high-confidence-max-price`、`--high-confidence-max-spread`、`--high-confidence-min-bid-depth`、`--high-confidence-stop-loss`、`--high-confidence-max-stop-slippage`、`--high-confidence-stop-mid-buffer`、`--high-confidence-breakeven-trigger`、`--high-confidence-trailing-stop`、`--high-confidence-confirm-up-ticks`、`--high-confidence-sample-seconds`、`--high-confidence-max-sample-gap-seconds`。

示例：

```powershell
py .\bot.py --strategy high-confidence --stake 4 --initial-cash 100
```

## Windows 启动

```powershell
cd D:\polymarket\polysimulator_btc5m_bot
py -m pip install -r requirements.txt
py .\bot.py
```

默认策略等价于：

```powershell
py .\bot.py --entry 0.60 --stake 4 --initial-cash 100
```

你提到的另一组规则（20¢ 入场、40¢ 止盈、10¢ 止损）可以单独跑一组：

```powershell
py .\bot.py --entry 0.20 --take-profit-price 0.40 --stop-price 0.10
```

每个策略最好用单独文件夹运行，避免共用模拟余额和交易记录。

## 记录文件

- `local_state.json`：虚拟现金、累计已实现盈亏和未平仓仓位。程序重启后会接着使用这里的状态；若要从 $100 重新开始，先停止程序，再删除这个文件。
- `local_trades.csv`：每笔模拟买入、卖出、费用、盈亏、现金余额和市场标识，可用 Excel 打开。卖出行还会保存触发时的 mid/bid/ask、最近成交价、盘口年龄、持仓时长和实际扫过的买盘档位；旧 CSV 会自动补上新列并保留原有记录。
- `pnl.log`：专门记录每次买入和卖出的盈亏信息，不混入行情、策略信号和运行错误。
- `pnl_summary.json`：专用盈亏汇总，包含账户累计盈亏、该专用记录器启动后的盈亏小计、胜负笔数、胜率、单笔盈亏、手续费和持仓时长；同时保留最近 200 笔明细。
- `bot.log`：每次启动后的英文运行提示、市场信号和错误信息，会持续追加保存。每次止盈/止损/到期退出会分别记录触发报价与模拟成交档位；盘口数据超过 5 秒未更新时，会跳过入场和常规止盈止损判断，直到收到新行情。

停止程序按 `Ctrl+C`。网络断开时程序会尝试重新连接。行情只包含 WebSocket 能提供的盘口数据；盘口深度、延迟、撮合和费用估算都会让模拟结果与实际交易不同。
