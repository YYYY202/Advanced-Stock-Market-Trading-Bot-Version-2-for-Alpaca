# BE 隔夜交易脚本：实盘设置

**风险提示**：这是用真钱做的实验，可能亏损。回测结果不代表未来，也没有扣交易成本。不构成投资建议。

## 策略
- **买入**：每个交易日纽约时间 15:35 到 15:49，用收盘单（MOC）买入。仓位上限是 `--max-usd` 美元，只买整股。
- **盘前卖出**：第二天纽约时间 04:05，挂盘前限价单卖出，北京时间是冬令时 17:05、夏令时 16:05。
  - **限价怎么定**：限价是"前一天收盘价减 2%"（`--floor`）。限价卖单会按当时最好的买价成交，只要不低于这个底价。所以价格正常时能卖在接近市价的位置，只有大幅低开超过 2% 时才挂着不成交。
  - **有新鲜报价时**：比如 08:00 以后，限价改用买一价减 0.3%（`--slip`），但不低于上面的底价。
  - **不用订阅 SIP**：前一天收盘价用 Alpaca 的 SIP 历史日线获取，免费账户也能用 15 分钟以前的数据。
- **兜底卖出**：盘前没卖掉的，09:15 到 09:27 会撤掉原来的单，改用开盘单（OPG）卖出。
- **动量信号期间不卖**：RSI(14) 上穿 70 后进入动量状态，直到 RSI 超过 80 或收盘价跌破 20 日均线才结束。这期间整天持有，不在早上卖出。
- **节假日不挂单**：当天不开盘就跳过。

### 融资账户
建议用融资（margin）账户：每天卖出后当天就能再买入，不会有现金账户的资金交割问题。

这套交易是今天收盘买、第二天卖，不算日内交易，所以不会触发日内交易限制（PDT）。仓位受 `--max-usd` 限制，不会用到融资借款。

## 安装（在你自己的电脑上：Linux 或 macOS）

1. 克隆仓库，安装依赖：
   ```bash
   git clone -b claude/be-stock-research-d8brk8 https://github.com/YYYY202/Advanced-Stock-Market-Trading-Bot-Version-2-for-Alpaca.git
   cd Advanced-Stock-Market-Trading-Bot-Version-2-for-Alpaca
   pip3 install requests      # 需要 Python 3.9 以上（要用 zoneinfo）
   chmod +x run-overnight-trader.sh
   ```

2. 创建密钥文件 `~/.be-trader.env`。不要把这个文件放进仓库：
   ```bash
   cat > ~/.be-trader.env <<'EOF'
   export APCA_API_KEY_ID=你的实盘KEY
   export APCA_API_SECRET_KEY=你的实盘SECRET
   export APCA_API_BASE_URL=https://api.alpaca.markets
   export BE_TRADER_ARGS="--live --max-usd 1000"
   EOF
   chmod 600 ~/.be-trader.env
   ```

3. 先查看账户状态，这一步不会下单：
   ```bash
   ./run-overnight-trader.sh status; tail -1 overnight-trader-cron.log
   ```

4. 试运行，只打印会下什么单，不真的发送。下面两条命令会输出"跳过：已收盘"，或者打印出准备发送的订单：
   ```bash
   source ~/.be-trader.env && python3 overnight-trader.py buy --live --max-usd 1000 --dry-run
   source ~/.be-trader.env && python3 overnight-trader.py sell-pre --live --dry-run
   ```

5. 加入定时任务：运行 `crontab -e`，加下面这一行，每 5 分钟跑一次，跟电脑的时区无关：
   ```
   */5 * * * * /完整路径/Advanced-Stock-Market-Trading-Bot-Version-2-for-Alpaca/run-overnight-trader.sh auto
   ```
   电脑需要保持开机、不休眠，尤其是纽约时间 15:35 到 15:49 和 04:05 到 09:27 这两段。

## 日常查看
- **04:00 卖和开盘卖的自动对比**：每个交易日纽约时间 09:50 到 10:10，`auto` 会自动记录当天实际卖出的成交价和 09:30 官方开盘价，存进 `overnight-trader-compare.csv`，每天只记一次。
  - **记录内容**：卖出方式（盘前，或开盘单兜底）、成交时间、两种卖法的收益率、每笔差多少百分比和美元。
  - **随时看汇总**：运行 `python3 overnight-trader.py report`，不需要密钥。会显示每笔明细、平均差距、t 值、盘前卖更好的天数占比、累计多赚或少赚的金额。
  - **怎么解读**：回测预期是盘前卖平均每笔多 0.24%，但 2026 年接近 0。至少要积累几十笔交易，平均值才有参考意义。
- `overnight-trader-log.jsonl`：每一次决策和下单的记录。
- `overnight-trader-cron.log`：程序运行输出和报错。
- 手动撤掉所有挂单：`./run-overnight-trader.sh cancel`
- **紧急停止**：注释掉 crontab 里那一行，然后到 Alpaca 网页上手动平仓。

## 安全机制
- 必须同时满足两个条件才会下实盘单：命令里带 `--live`，并且 `APCA_API_BASE_URL` 设成实盘地址。两者对不上就拒绝运行。
- 已经持仓，或者已有挂单时，不会再买入。
- 仓位不超过 `--max-usd` 美元。
- 每一步都可以重复执行，不会因为重复运行而重复下单。
