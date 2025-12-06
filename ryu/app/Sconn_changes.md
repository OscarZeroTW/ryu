# SconnControllerV9 — 變更紀錄與部署說明

## 摘要
這份文件說明對 `ryu/app/sconn.py` 的主要修改、為什麼修改、以及部署到實體環境時應注意的事項與測試指引。

---

## 主要修改（重點）

- 正確儲存拓撲連線的雙向端口資訊
  - 原先只保存單向 `port`，現在於 graph edge 中儲存 `ports={dpid1: port1, dpid2: port2}`。
  - 位置：`topology_change_handler` 中的 `EventLinkAdd` 處理。

- 移除重複的雙向 add_edge / remove_edge 呼叫
  - 因為使用 `nx.Graph()`（無向圖），不需要手動重複加入相反方向的邊。

- 改善洪泛（flood）邏輯與流表安裝
  - 不為多端口的洪泛安裝永久流表（避免在交換機層產生固定環路）。
  - 只在已知單播目的地時安裝單播流表以加速後續轉發。

- 對非-STP 交換機互連端口，於 switch 層面安裝阻擋規則
  - 檢查 `stp_net`（minimum spanning tree）並為非樹邊的互連端口安裝高優先級 drop flow（priority=10），以避免硬體層面的環路。

- 過濾及處理噪音性封包
  - LLDP（拓撲探測封包）直接過濾（不做後續處理）。
  - IPv6/IPv4 多播（開頭 `33:33`、`01:00:5e`）設為安裝 drop 規則以避免大量 `PacketIn`。

- 添加安全檢查與錯誤避免
  - 在存取 `self.datapaths[dpid]` 前確認 datapath 已註冊，避免 KeyError。

---

## 為什麼要這樣改

- 保留雙向端口資訊可準確判斷「哪個端口連到哪個交換機」，必要於 STP 與洪泛決策。
- 不在洪泛時安裝流表可避免交換機層面的固定循環路徑，STP 應當決定洪泛的方向。
- 在交換機上阻擋非-STP 互連端口能於硬體層級就切斷可能的環路，降低控制器與網路風暴風險。
- 過濾多播/LLDP 可顯著降低控制器的噪音日誌與不必要事件負載。

---

## 部署到實體環境可能出現的問題與建議

- 手動加入的 wireless link（硬編碼）：
  - 目前範例中有一段 `self.switch_net.add_edge(2, 3, ports={2: 6, 3: 6})`。
  - 問題：實體環境的 switch ID 與端口號通常不同。硬編碼會造成錯誤或無效行為。
  - 建議：移除或改為透過設定檔/環境變數注入，或完全依賴 LLDP 自動發現。

- 多播封包全部丟棄的風險：
  - 某些服務（mDNS, IGMP, DHCP relay, IPv6 NDP）依賴多播或特定多播地址。
  - 建議：針對需要的多播類型做允許白名單（例如允許 DHCP、IGMP），僅阻擋不必要的多播來源。

- 流表 timeout 與頻繁重學習：
  - 範例中單播流表使用 `idle=10, hard=30`（較短）。實體網路建議較長的 timeout（例如 idle=300, hard=600）。
  - 建議：依實際流量型態調整，避免控制器過度負載。

- 清除所有 flow 的衝擊：
  - 當 link 刪除時目前會清掉所有交換機的 flow（強制重學習），可能導致流量瞬間中斷。
  - 建議：只針對受影響路徑刪除，或採分批更新並使用 Barrier 以確保一致性。

- 控制器 HA 與故障恢復：
  - 目前僅單一控制器，實體部署應考慮高可用（HA）設計或交換機端 fail-secure 設定。

- 權限與安全：
  - 需要考慮管理平面與資料平面的隔離、API 的存取控制等。

---

## 測試建議（Mininet / 實體）

1. 啟動控制器（推薦不使用 `--verbose`）並啟用 observe-links：

```bash
# 在開發機上（已建立虛擬環境）
source ~/ryu-env/bin/activate
ryu-manager --observe-links ryu.app.gui_topology.gui_topology '/home/ubuntu/ryu/ryu/app/sconn.py'
```

2. Mininet 基本測試：

```bash
sudo mn --controller=remote --topo linear,3 --switch ovs,protocols=OpenFlow13 --mac
# 在 mininet> 提示下
mininet> h1 ping -c 3 h2
mininet> sh ovs-ofctl -O OpenFlow13 dump-flows s1
```

- 觀察 controller 日誌是否有 "Learned/Updated MAC" 以及 "Installing flow" 訊息。
- ping 前幾個封包會觸發 `PacketIn`（學習），之後應由交換機直接轉發（無 `PacketIn`）。

3. 環路測試（手動建立 s1<->s3 連線）：

```bash
# 在 mininet> 提示下
mininet> py net.addLink(s1, s3)
mininet> py s1.attach('s1-eth3')
mininet> py s3.attach('s3-eth3')
mininet> pingall
```

- 觀察是否有廣播風暴；如果有，檢查控制器日誌中是否顯示被安裝的 "Blocking non-STP port" 訊息。若未顯示，代表拓撲資訊或 STP 計算有誤。

4. 多播相關測試：
- 若你的網路需要 mDNS/IGMP/DHCP relay 等，測試這些服務在啟用 drop 規則後是否仍可運作；如有問題，調整白名單。

---

## 推薦改善方向（中長期）

- 將靜態或硬編碼設定（如特定 switch id / port）抽成設定檔或 CLI 參數。
- 實作選擇性多播白名單（根據協定、來源或 VLAN）。
- 更細緻的流表更新策略（只移除受影響流表），並使用 Barrier Request/Reply 保證變更順序。
- 增加監控（flow stats、port stats）以自動偵測洪泛或異常流量。
- 考慮控制器集群以提升可用性。

---

## 檔案位置

已將此說明存成：
`ryu/app/Sconn_changes.md`

---

如需我把說明改成英文、或把重點摘成短報告（例如 README 條列式摘要）我可以再產出版本。也可以依照您的實體環境提供一份參數化的部署建議清單（例如把 switch-id 與端口配置成 YAML）。
