# BE 隔夜交易脚本：实盘设置

**风险提示**：这是用真钱做的实验，可能亏损。回测结果不代表未来，也没有扣交易成本。不构成投资建议。

## 策略
- **买入**：每个交易日纽约时间 15:35 到 15:49，用收盘单（MOC）买入。仓位上限是 `--max-usd` 美元，只买整股。
- **盘前卖出**：第二天纽约时间 04:05 到 09:10，挂盘前限价单卖出，限价是买一价减 0.3%（`--slip`）。
- **兜底卖出**：盘前没卖掉的，09:15 到 09:27 会撤掉原来的单，改用开盘单（OPG）卖出。
- **动量信号期间不卖**：RSI(14) 上穿 70 后进入动量状态，直到 RSI 超过 80 或收盘价跌破 20 日均线才结束。这期间整天持有，不在早上卖出。

### 用免费数据时的卖出时间
Alpaca 免费的 IEX 行情在盘前要到**纽约时间 08:00** 才有报价。脚本发现报价超过 15 分钟没更新，就不会挂单，会等下一轮再试。所以用免费数据时，实际卖出时间大约在 08:00 以后。

想在 04:00 一开盘就卖，需要订阅 Alpaca 的 SIP 行情，然后在参数里加 `--feed sip`。

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
- `overnight-trader-log.jsonl`：每一次决策和下单的记录。
- `overnight-trader-cron.log`：程序运行输出和报错。
- 手动撤掉所有挂单：`./run-overnight-trader.sh cancel`
- **紧急停止**：注释掉 crontab 里那一行，然后到 Alpaca 网页上手动平仓。

## 安全机制
- 必须同时满足两个条件才会下实盘单：命令里带 `--live`，并且 `APCA_API_BASE_URL` 设成实盘地址。两者对不上就拒绝运行。
- 已经持仓，或者已有挂单时，不会再买入。
- 仓位不超过 `--max-usd` 美元。
- 每一步都可以重复执行，不会因为重复运行而重复下单。
