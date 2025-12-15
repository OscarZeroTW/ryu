# sconn_old.py vs sconn.py 差異比較

本文件描述 `sconn_old.py` 與 `sconn.py` 之間的具體差異。

---

## 差異摘要

| 項目 | sconn_old.py | sconn.py |
|------|--------------|----------|
| 圖資料結構 | `nx.Graph()` (無向圖) | `nx.DiGraph()` (有向圖) |
| 連結權重 | 無 | 有 (有線=1, 無線=10) |
| 無線連結保護 | 無 | 有 |
| Table-miss 規則重建 | 無 | 有 |
| Datapath 未註冊檢查 | 無 | 有 |
| Flow 超時設定 | idle=10s, hard=30s | idle=3s, hard=10s |

---

## 詳細差異

### 1. 網路拓撲資料結構

**sconn_old.py (第 19 行)**
```python
self.switch_net = nx.Graph()
```

**sconn.py (第 19 行)**
```python
self.switch_net = nx.DiGraph()  # Use directed graph for bidirectional port mapping
```

**說明**：新版改用有向圖 (`DiGraph`)，可以更精確地記錄雙向連結各自的 port 資訊。

---

### 2. 連結權重系統

**sconn_old.py** - 無權重系統

**sconn.py** - 引入權重機制

| 連結類型 | 權重 |
|----------|------|
| 有線連結 (Wired) | 1 |
| 無線連結 (WiFi) | 10 |

```python
# 無線連結 (第 66-67 行)
self.switch_net.add_edge(2, 3, port=6, weight=10)
self.switch_net.add_edge(3, 2, port=6, weight=10)

# 有線連結 (第 74-75 行)
self.switch_net.add_edge(link.src.dpid, link.dst.dpid, port=link.src.port_no, weight=1)
self.switch_net.add_edge(link.dst.dpid, link.src.dpid, port=link.dst.port_no, weight=1)
```

**用途**：MST 計算時優先選擇權重較低的有線連結，無線連結作為備援。

---

### 3. Spanning Tree 計算方式

**sconn_old.py (第 47-50 行)**
```python
def _calculate_stp(self):
    if self.switch_net.nodes:
        self.stp_net = nx.minimum_spanning_tree(self.switch_net)
```

**sconn.py (第 47-54 行)**
```python
def _calculate_stp(self):
    if self.switch_net.nodes:
        # Convert DiGraph to undirected Graph for spanning tree calculation
        undirected = self.switch_net.to_undirected()
        # Use 'weight' attribute to prefer wired links
        self.stp_net = nx.minimum_spanning_tree(undirected, weight='weight')
```

**說明**：新版需先將有向圖轉為無向圖，並使用 `weight` 參數讓 MST 演算法考慮權重。

---

### 4. 無線連結保護機制

**sconn_old.py** - 無保護，所有 `EventLinkDelete` 事件都會處理

**sconn.py (第 80-87 行)** - 忽略無線連結的刪除事件

```python
# IMPORTANT: Protect the manually added wireless link (2-3) from being deleted
is_wireless_link = (link.src.dpid == 2 and link.dst.dpid == 3) or \
                   (link.src.dpid == 3 and link.dst.dpid == 2)

if is_wireless_link:
    self.logger.info("Ignoring delete event for wireless link 2 <--> 3 (manually managed)")
    return
```

**說明**：由於無線連結沒有 LLDP，拓撲模組可能會發送錯誤的刪除事件，新版會忽略這些事件。

---

### 5. Table-miss 規則重建

**sconn_old.py** - 刪除所有流表後不重建 table-miss 規則

**sconn.py (第 105-109 行)** - 刪除流表後立即重建

```python
# Reinstall table-miss rule to ensure controller can continue receiving packets
match = parser.OFPMatch()
actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                  ofproto.OFPCML_NO_BUFFER)]
self.add_flow(dp, 0, match, actions)
```

**說明**：確保控制器在清除流表後仍能持續接收封包。

---

### 6. Datapath 未註冊時的處理

**sconn_old.py** - 直接存取 `self.datapaths[dpid]`，可能導致 `KeyError`

**sconn.py (第 145-153 行)** - 新增安全檢查

```python
# Prevent KeyError: check if dpid is already in datapaths
if dpid not in self.datapaths:
    self.logger.warning("Datapath %s not yet registered, using simple flood.", dpid)
    actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
    data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
    out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                              in_port=in_port, actions=actions, data=data)
    datapath.send_msg(out)
    return
```

**說明**：避免在 datapath 尚未完全註冊時發生錯誤，改用簡單的 FLOOD 作為備援。

---

### 7. Flow 超時設定

**sconn_old.py (第 148 行)**
```python
self.add_flow(datapath, 1, match, actions, idle=10, hard=30)
```

**sconn.py (第 180 行)**
```python
# Shorter timeout for faster failover (idle=3s, hard=10s)
self.add_flow(datapath, 1, match, actions, idle=3, hard=10)
```

| 參數 | sconn_old.py | sconn.py |
|------|--------------|----------|
| idle_timeout | 10 秒 | 3 秒 |
| hard_timeout | 30 秒 | 10 秒 |

**說明**：縮短超時時間可加速故障轉移 (failover)，讓網路在連結失效時更快恢復。

---

## 總結

`sconn.py` 相對於 `sconn_old.py` 的改進：

1. **更精確的拓撲建模** - 使用有向圖正確記錄雙向 port
2. **智慧路徑選擇** - 透過權重機制優先使用有線連結
3. **更強健的無線連結管理** - 保護手動新增的無線連結不被誤刪
4. **更完善的錯誤處理** - 防止 KeyError 和 table-miss 規則遺失
5. **更快的故障恢復** - 縮短 flow 超時以加速 failover

