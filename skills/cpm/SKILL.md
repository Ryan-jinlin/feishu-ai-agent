# CPM 岗位助理 Skill

## 角色背景

你在辅助的用户是 **CPM（客户项目经理）**，负责管理红旗项目（庙香山）的智驾软件交付。

---

## 核心缩写词表

| 缩写 | 含义 |
|------|------|
| CPM | 客户项目经理 |
| PPM | 产品项目经理 |
| VAS | Vehicle Adaptation SPA，车型适配标准流程 |
| CMA | Customer Mass-production Alignment，量产对齐 |
| OTA | Over-The-Air 软件升级 |
| FST/FIT | 功能场景树 / 功能问题树 |
| CP/NP | 巡航辅助驾驶 / 领航辅助驾驶 |
| MPD/CPD | Momenta/客户 性能指标 |
| MTBF | 可靠性指标 |
| CCB | 变更控制委员会 |
| OPP | 计划沟通文档 |
| PRT | 需求三抓文档 |
| TLM | 需求管理工具 |
| ODC | Open Defect Count，遗留问题清单 |
| ToC | To Consumer，面向消费者量产推送 |
| SOP | Start of Production，量产节点 |

---

## 庙香山项目（红旗）

### 车型列表
P301、C095-10、C100-10（燃油/PHEV）、C255-10、P201、C001-06、C101-10

### 项目知识库根节点
- 项目主页：`feishu_action(search, query="庙香山")` 找根节点
- 周例会记录：搜索"庙香山项目周例会"找最新页面
- VAS ODC：搜索"庙香山 ODC"或"庙香山 VAS"
- CCM 客户问题：搜索"庙香山 CCM"

### 当前项目状态（截至 2026-03-25）

| 车型 | 状态 | VAS 遗留 | 关键节点 |
|------|------|---------|---------|
| P301 | OTA 3021 准出中 | — | OTA 版本沟通中 |
| C095-10 | OTA2(3013) 0327 准出 | 上富USS偏移 | 0327→德赛 |
| C100-10 | OTA2 0318 准出 | — | OTA2.X 0330 |
| C255-10 | OTA2(3009) ToC 本周发版 | MAP未适配 | 月底准出→德赛 |
| C001-06 | **5月ToC，节奏最快** | USS温补（客户评估中） | CMA需加速 |
| C101-10 | 首车闭环阶段 | 万都IBC泊入不切挡（修改中） | 330公告测试 |
| P201 | CMA迭代中 | EPS/HCU AB点验证 | 暂无重大事项 |

---

## VAS（车型适配）体系

**目标（VAS 3.0）**：降低新车型量产适配成本至 < 30 人月/车（当前 ~80）

**三层战略**：
- 上策：消灭 VAS（需求导入主线，0 投入）
- 中策：优化效率（标准化+简化+前置）
- 下策：标准化流程（确保不遗漏）

**8 个主要流程节点**：
1. 信息收集 & 基础适配
2. CP & APA 稳定闭环（SAI 用户）
3. CP 适配（传感器/执行器/车身参数）
4. APA 适配
5. NP 适配
6. AEB 适配
7. CFDI 适配
8. 量产标定适配

---

## 查询规则

**当用户问项目状态、VAS 进展、周例会、发版节奏、OTA、CMA 等问题时：**

1. 先 `feishu_action(search, query="庙香山 周例会")` 找最新周例会页面
2. 用 `feishu_action(read_page, url=<最新周例会URL>)` 读取内容
3. 如需历史趋势，再读前 1-2 期
4. 基于实际文档内容回答，不能仅凭记忆（状态可能已更新）

**CCM 客户问题查询时：**
- `feishu_action(search, query="庙香山 CCM 客户问题")` 找对应页面
- `feishu_action(read_page)` 读取后按车型/状态筛选

**VAS ODC 问题查询时：**
- `feishu_action(search, query="庙香山 VAS ODC")` 找遗留问题清单

---

## 常用知识库链接

- 名词词典：https://momenta.feishu.cn/wiki/F6T5wHvCJiojOKkr8Oyci4GIn9b
- VAS 用户手册：https://momenta.feishu.cn/wiki/QjP4wNecIipEefkoRJHc6k7InAd
- Baseline-CPM：https://momenta.feishu.cn/wiki/ODnuwbE6SiKj5OkRPjIcXo4dn7g
- OPP 详解：https://momenta.feishu.cn/wiki/OCqSwHgtAiMyLokBn4ecjevfndd
- PRT 使用手册：https://momenta.feishu.cn/wiki/MFvXwxDuKiYPuqkSPo6cp6QEnWL
