---
title: 'Agent memory 2025-2026 vs. backend đang chạy (kiểm toán rộng)'
type: 'research'
created: '2026-08-15'
status: 'living'
context:
  - 'document/rag-workflow/2.multi-turn-memory.md'
  - 'document/rag-workflow/2.lesson.md'
  - 'document/rag-workflow/3.lesson.md'
  - 'document/rag-workflow/4.memory.md'
---

# Bài học từ 10 paper agent memory, đối chiếu với code thật

Cập nhật: 2026-08-15.

`2.lesson.md` đối chiếu paper với **thiết kế** trong `2.multi-turn-memory.md`. Tài liệu này khác ở hai chỗ:

1. Bộ paper khác. Lần này lọc bằng tiêu chí cứng: citation > 100 (Semantic Scholar, 2026-08-15) **và** có venue lớn thật, verify qua OpenReview API + arXiv metadata. Bảy trong mười paper chưa xuất hiện ở `2.lesson.md`.
2. Đối chiếu với **code đang chạy**, không phải với thiết kế. Mọi khẳng định về backend đều có `file:line`.

Kết quả quan trọng nhất không nằm ở paper. Nó nằm ở chỗ code đã trôi khỏi thiết kế: **toàn bộ đường ghi memory theo `memory_routing` đã chết**, và scope bây giờ do model tự chọn, đúng cái mà `memory/routing.py:1-25` viết là không đáng tin.

> **Đọc như một biên bản kiểm toán, không phải mô tả hiện trạng.** Mọi `file:line` dưới đây đúng với code ngày 2026-08-15, trước đợt tách thư mục — `summary/summarizer.py` nay là package `summary/`, `storage/{memory,pg_memory}.py` và `memory/routing.py` nay là `memory/`.
>
> **File này không đánh số vì nó bắc qua nhiều bước.** Phần nghiên cứu chuyên sâu đã tách ra:
>
> | Chủ đề | Nghiên cứu | Thiết kế / hiện trạng |
> |---|---|---|
> | Thread summary | [`3.lesson.md`](3.lesson.md) — đào sâu ACE từ full text | [`3.thread-summary.md`](3.thread-summary.md) |
> | Memory store | (còn ở đây) | [`4.memory.md`](4.memory.md) |
>
> Trạng thái 7 phát hiện ở §3: **§3.3** (summary tự viết lại) và **§3.5** (cắt overflow theo bảng chữ cái) **đã sửa**. **§3.1** (`memory_routing` chết), **§3.2** (`last_used_at`), **§3.4** (không xoá được memory), **§3.6** (không đo gì), **§3.7** (code chết) — kiểm lại với `4.memory.md` trước khi trích, vì memory đã được viết lại sau bản kiểm toán này.

---

## 1. Backend hiện tại là gì

Đọc từ code, không từ doc.

### 1.1 Đường ghi (write path)

Chỉ còn **một** đường sống: model sinh câu trả lời tự gọi tool.

- [memory/tools.py:115-171](../../backend/rag_ingestion/memory/tools.py#L115-L171) dựng 2 tool cho mỗi lượt: `save_user_memory` và `save_project_memory` / `save_conversation_memory`. Scope nằm trong **tên tool**, không phải tham số.
- [api/routers/threads.py:255-268](../../backend/rag_ingestion/api/routers/threads.py#L255-L268) gắn tool vào lời gọi generation. Ghi xảy ra trong lúc stream.
- [memory/tools.py:50-63](../../backend/rag_ingestion/memory/tools.py#L50-L63) `normalize_key` map alias (`profession` → `occupation`), phần còn lại rơi xuống `other:<slug>`.
- [api/deps.py:731-765](../../backend/rag_ingestion/api/deps.py#L731-L765) `_ContextMemoryWriter` dedup bằng `recalled` đã load sẵn, không query thêm.

### 1.2 Đường ghi đã chết

- [memory/routing.py](../../backend/rag_ingestion/memory/routing.py) — `route()`, `scope_for()`, `should_promote()`.
- [api/deps.py:625-728](../../backend/rag_ingestion/api/deps.py#L625-L728) — `write_understanding` / `_write_understanding`.
- [understanding.py:25](../../backend/rag_ingestion/understanding.py#L25) `_MEMORY_CAP = 3` và [understanding.py:54-73](../../backend/rag_ingestion/understanding.py#L54-L73) parse `memory[]`.

Không route nào gọi `write_understanding`. Và [prompts/understanding.py](../../backend/rag_ingestion/prompts/understanding.py) chỉ còn yêu cầu `rewritten_query`, nên `understanding.memory` luôn rỗng. Test `tests/test_memory/routing.py` vẫn xanh vì nó test hàm thuần, không test đường đi thật.

Ba hệ quả:

| Mất gì | Ở đâu |
|---|---|
| Scope quyết định bằng `key`, không bằng model | `memory/routing.py:60-61` |
| Auto-promote thread → user khi key xuất hiện ở 2 thread | `deps.py:710-728`, `pg_memory.py:290-307` |
| `distinct_thread_count` không còn ai gọi | `pg_memory.py:290` |

### 1.3 Đường đọc (read path)

- [pg_memory.py:145-185](../../backend/rag_ingestion/memory/store.py#L145-L185) `recall()` — load **toàn bộ** row sống của 3 scope, `ORDER BY key`. Không retrieval, không ranking, không embedding.
- [retrieval/answering.py:51-76](../../backend/rag_ingestion/retrieval/answering.py#L51-L76) `_resolve()` — mỗi key giữ 1 memory, scope hẹp thắng, có ghi chú "this conversation only" khi override.
- [retrieval/answering.py:85-117](../../backend/rag_ingestion/retrieval/answering.py#L85-L117) `_memory_sections()` — render tới khi hết 1000 ký tự ([config.py:94](../../backend/rag_ingestion/config.py#L94)), phần thừa **báo số lượng bị rớt** chứ không im lặng.
- [retrieval/answering.py:38-48](../../backend/rag_ingestion/retrieval/answering.py#L38-L48) `_memory_age()` — thêm "(saved N days ago)" để model biết nghi ngờ.

### 1.4 Conflict, cap, xoá

- [pg_memory.py:675-726](../../backend/rag_ingestion/memory/store.py#L675-L726) `_claim_slot` — advisory lock theo (owner, scope, target), check cap và supersede trong cùng một transaction. Row cũ cùng key bị set `superseded_by` + `deleted_at`.
- Cap: thread 10 / project 30 / user 30 ([memory/models.py:68-72](../../backend/rag_ingestion/memory/models.py#L68-L72)). Đầy → `ValueError` → chuyển thành proposal.
- Delete là soft delete ([pg_memory.py:264-288](../../backend/rag_ingestion/memory/store.py#L264-L288)), có audit trail theo `sequence` ([pg_memory.py:772-801](../../backend/rag_ingestion/memory/store.py#L772-L801)).

### 1.5 Summary (L2)

- [summary/summarizer.py:47-86](../../backend/rag_ingestion/summary/summarizer.py#L47-L86) — incremental: `previous summary + turns mới rớt ra` → **viết lại toàn bộ summary**, trần 400 token.
- Cửa sổ: giữ 6 message gần nhất, summarize khi ≥ 8 message nằm ngoài ([config.py:63-76](../../backend/rag_ingestion/config.py#L63-L76)).

### 1.6 Tổng kết một dòng

Backend là **flat key-value store có scope, có supersede, có audit, đọc bằng load-all**, cộng một summary prose viết lại mỗi vòng. Không có link, không có embedding trên memory, không có usage signal, không có procedural memory.

---

## 2. Mười paper: phương pháp và đối chiếu

Ký hiệu mức bằng chứng dùng chung với `1.lesson.md` và `2.lesson.md`.

### 2.1 A-MEM: Agentic Memory for LLM Agents

- **Trạng thái:** NeurIPS 2025 Poster. 873 citation. [arXiv:2502.12110](https://arxiv.org/abs/2502.12110) · [NeurIPS](https://neurips.cc/virtual/2025/poster/119020)
- **Phương pháp:** Zettelkasten. Mỗi memory mới sinh một *note* có cấu trúc: contextual description, keywords, tags. Hệ thống quét note cũ tìm liên hệ và **tạo link**. Quan trọng hơn: *memory evolution* — note mới có thể trigger cập nhật lại description/tag của note cũ. Tổ chức không cố định, LLM quyết cấu trúc.
- **So với backend:** backend không có link, không có tag, không có evolution. Nhưng backend có thứ A-MEM không có: **closed key list** ([memory/models.py:44-61](../../backend/rag_ingestion/memory/models.py#L44-L61)). Hai cái này giải cùng một bài toán — làm sao hai memory mâu thuẫn không cùng sống — bằng hai giá rất khác. A-MEM trả 1 LLM call mỗi lần insert. Backend trả 1 câu `WHERE key = ?`.
- **Học được:** `keywords`/`tags` của A-MEM đáng lấy, phần link thì không. Backend đang render memory bằng `key: value` trần ([answering.py:79-82](../../backend/rag_ingestion/retrieval/answering.py#L79-L82)); thêm được ngữ cảnh ngắn ("nói khi đang hỏi về X") sẽ giúp model biết memory này áp dụng lúc nào. Đó là phần rẻ của A-MEM.
- **Mức bằng chứng:** **Trung bình**. Số chính trên LoCoMo, và §5 dưới đây giải thích tại sao phải giảm trọng số.

### 2.2 Mem0

- **Trạng thái:** ECAI 2025. 547 citation. [arXiv:2504.19413](https://arxiv.org/abs/2504.19413) · [IOS Press](https://ebooks.iospress.nl/volumearticle/76096)
- **Phương pháp:** hai pha. *Extraction* rút fact từ cặp hội thoại. *Update* lấy top-k memory tương tự rồi để LLM chọn một trong bốn: ADD / UPDATE / DELETE / NOOP. Biến thể `Mem0^g` lưu thành graph. Báo cáo p95 latency thấp hơn 91%, token giảm hơn 90% so với full-context.
- **So với backend:** backend có ADD (tool) và DELETE (chỉ qua API người dùng, [api/routers/memory.py](../../backend/rag_ingestion/api/routers/memory.py)). **Không có đường tự động DELETE hay UPDATE**. `_claim_slot` làm UPDATE ngầm bằng supersede, nhưng chỉ khi key trùng chính xác. Nếu user nói "tôi nghỉ việc ở FPT rồi", model không có cách nào xoá `organization` — nó chỉ có thể ghi đè bằng một giá trị mới, và nếu nó không nghĩ ra giá trị mới thì fact cũ sống mãi.
- **Học được:** đây là gap cụ thể nhất, và rẻ nhất để đóng: thêm tool `forget_memory(key)` map sang `delete_memory`. Bốn operation của Mem0 khớp đúng `MemoryOperation` đã có sẵn trong enum ([memory/models.py:81-85](../../backend/rag_ingestion/memory/models.py#L81-L85)) — enum có ADD/UPDATE/DELETE nhưng đường tự động chỉ dùng ADD.
- **Mức bằng chứng:** **Thấp** cho con số. Paper của chính công ty bán sản phẩm, LLM-as-judge với prompt bảo judge "be generous", và Zep đã phản biện công khai. Cao cho *thiết kế bốn operation*, vì Memory-R1 (§2.9) tái xác nhận độc lập.

### 2.3 LongMemEval

- **Trạng thái:** ICLR 2025. 525 citation. [arXiv:2410.10813](https://arxiv.org/abs/2410.10813)
- **Phương pháp:** benchmark 500 câu trên 5 năng lực — information extraction, multi-session reasoning, temporal reasoning, **knowledge updates**, **abstention**. Kèm khung 3 pha *indexing → retrieval → reading* và ba tối ưu đo được: session decomposition (chia nhỏ đơn vị giá trị), fact-augmented key expansion (index bằng fact đã trích, không bằng chat thô), time-aware query expansion.
- **So với backend:** backend đã làm đúng *fact-augmented* — `value` là fact ngắn do model viết, không phải đoạn chat cắt ra. Nhưng backend **không có pha retrieval**: `recall()` load hết. Với cap 30+30+10 thì load-all đúng và rẻ hơn retrieval; đây là chỗ backend nên giữ nguyên, không copy.
- **Học được:** cái thiếu là **abstention** và **temporal**. Prompt hiện tại ([prompts/answering.py](../../backend/rag_ingestion/prompts/answering.py)) không nói gì về việc từ chối trả lời khi memory không đủ. `_memory_age` đã là bước đi đúng hướng cho temporal nhưng chỉ dừng ở một dòng chú thích.
- **Mức bằng chứng:** **Cao**. Peer-review, curated chứ không sinh tự động hoàn toàn, và là benchmark ít bị bão hoà nhất trong nhóm.

### 2.4 ACE: Agentic Context Engineering

- **Trạng thái:** ICLR 2026. 254 citation. [arXiv:2510.04618](https://arxiv.org/abs/2510.04618)
- **Phương pháp:** coi context như một *playbook tiến hoá*. Ba vai tách bạch: Generator chạy task, Reflector rút bài học từ execution feedback, Curator gộp vào playbook. Điểm mấu chốt: cập nhật **delta từng bullet có id**, không bao giờ viết lại toàn bộ. Lý do là hai failure mode được đặt tên rõ:
  - *brevity bias* — LLM viết lại thì tự động nén, và cái bị nén đi luôn là chi tiết miền.
  - *context collapse* — viết lại nhiều vòng thì thông tin bào mòn dần, không có vòng nào sai rõ ràng để phát hiện.
  - Kết quả: +10.6% agent, +8.6% finance, giảm latency thích nghi. Không cần label, chỉ cần execution feedback.
- **So với backend:** [summary/summarizer.py:52-86](../../backend/rag_ingestion/summary/summarizer.py#L52-L86) **chính xác là kiến trúc ACE cảnh báo**. Mỗi vòng fold: đưa summary cũ + turn mới vào, model viết lại toàn bộ summary, trần cứng 400 token. Thread 100 lượt sẽ đi qua khoảng 10 vòng viết lại. Không có cơ chế nào giữ chi tiết từ vòng 1 sống tới vòng 10 ngoài việc hy vọng model chép lại.
- **Học được:** **đây là finding mạnh nhất trong toàn bộ tài liệu.** Sửa được mà không cần model mới, không cần train: đổi summary từ prose sang danh sách item có id, mỗi vòng chỉ cho model phát ADD/UPDATE/KEEP trên từng item. Trần 400 token chuyển thành trần số item. Kèm thêm: có id thì log được item nào bị xoá ở vòng nào, tức là quan sát được collapse thay vì đoán.
- **Mức bằng chứng:** **Cao** cho cơ chế (failure mode mô tả rõ, có ablation delta vs rewrite). **Trung bình** cho con số tuyệt đối.

### 2.5 Agent Workflow Memory (AWM)

- **Trạng thái:** ICML 2025. 232 citation. [arXiv:2409.07429](https://arxiv.org/abs/2409.07429)
- **Phương pháp:** procedural memory. Rút *workflow* dùng lại được từ trajectory đã thành công, offline (từ train set) hoặc online (từ chính test query), rồi bơm workflow liên quan vào prompt. WebArena +51.1% relative success, Mind2Web +24.6%, và số bước giảm.
- **So với backend:** backend chỉ có `preference | fact | pin` ([memory/models.py:31-36](../../backend/rag_ingestion/memory/models.py#L31-L36)). Không có chỗ nào lưu "cách làm".
- **Học được:** bản dịch đúng cho RAG chat **không phải** workflow web. Nó là *retrieval procedure*: query nào cần rewrite kiểu gì, doc nào từng trả lời được câu hỏi loại nào, filter nào từng đúng. Hiện tại `rewritten_query` sinh mới mỗi lượt ([threads.py:151-159](../../backend/rag_ingestion/api/routers/threads.py#L151-L159)) và vứt đi ngay sau khi retrieve. Đó là dữ liệu procedural đang bị đổ bỏ.
- **Ưu tiên:** thấp. Chỉ đáng làm sau khi có eval đo được (§4). Không có eval thì không phân biệt được nó giúp hay chỉ thêm nhiễu.
- **Mức bằng chứng:** **Cao** trong miền web agent. **Chưa xác minh** khi chuyển sang chat RAG.

### 2.6 HippoRAG 2 (From RAG to Memory)

- **Trạng thái:** ICML 2025. 204 citation. [arXiv:2502.14802](https://arxiv.org/abs/2502.14802)
- **Phương pháp:** Personalized PageRank trên KG mở, cộng passage integration sâu hơn và một bước LLM lọc triple online. Điểm đáng chú ý là *động cơ*: các hệ KG-RAG trước đó mạnh về associativity nhưng **tụt hẳn ở factual QA**; HippoRAG 2 sửa đúng chỗ đó, +7% associative memory mà không hy sinh factual.
- **So với backend:** backend không có graph memory và **không nên có**. Đây là corpus memory, cùng họ với retrieval pipeline hiện có, không phải user memory.
- **Học được:** bài học thật là bài học phương pháp — thêm cấu trúc vào memory thường trả giá ở task đơn giản nhất, và cái giá đó chỉ hiện ra nếu có đo. Áp thẳng vào backend: nếu thêm tag/link kiểu A-MEM thì phải đo lại cả trường hợp tầm thường "user hỏi tên tôi là gì".
- **Mức bằng chứng:** **Cao**, nhưng **ngoài phạm vi** memory người dùng.

### 2.7 MemAgent

- **Trạng thái:** ICLR 2026 **Oral**. 192 citation. [arXiv:2507.02259](https://arxiv.org/abs/2507.02259)
- **Phương pháp:** RL (mở rộng DAPO sang multi-conversation). Agent đọc văn bản theo segment, mỗi bước **ghi đè** một memory kích thước cố định. Train ở 8K, ngoại suy 32K → 3.5M token với sụt < 5%; 95%+ trên RULER 512K.
- **So với backend:** khác miền. Đây là memory trong một task đọc dài, không phải memory xuyên phiên.
- **Học được:** một điều duy nhất, nhưng đúng — *overwrite policy học được thắng heuristic cắt cửa sổ*. Backend đang cắt bằng hằng số: giữ 6 message gần nhất ([config.py:68](../../backend/rag_ingestion/config.py#L68)), 24.000 ký tự, 40 message. Sáu là con số chọn tay. Không train được thì ít nhất phải đo: thread nào bị mất thông tin ở ranh giới cửa sổ.
- **Mức bằng chứng:** **Cao** cho long-context. **Không áp dụng trực tiếp**.

### 2.8 MemoryAgentBench

- **Trạng thái:** ICLR 2026 Poster. 188 citation. [arXiv:2507.05257](https://arxiv.org/abs/2507.05257)
- **Phương pháp:** benchmark định nghĩa 4 năng lực cốt lõi của *memory agent*: accurate retrieval, test-time learning, long-range understanding, **selective forgetting**. Chuyển dataset long-context sang dạng multi-turn tăng dần, mô phỏng đúng cách agent tích luỹ thông tin.
- **So với backend:** đối chiếu thẳng bốn trục:

| Năng lực | Backend | Bằng chứng |
|---|---|---|
| Accurate retrieval | Có (load-all, không thể miss) | `pg_memory.py:145` |
| Long-range understanding | Một phần (summary L2) | `summary/summarizer.py:47` |
| Test-time learning | **Không** | không có procedural memory |
| Selective forgetting | **Không** | xem §3.2 |

- **Học được:** dùng đúng 4 trục này làm bộ eval. Ba trong bốn đo được bằng test tích hợp, không cần dataset ngoài.
- **Mức bằng chứng:** **Trung bình**. Peer-review, nhưng dataset chuyển đổi từ long-context nên hơi nhân tạo.

### 2.9 MEM1

- **Trạng thái:** ICLR 2026 Poster. 184 citation. [arXiv:2506.15841](https://arxiv.org/abs/2506.15841)
- **Phương pháp:** RL end-to-end. Mỗi lượt agent cập nhật một *internal state kích thước hằng* vừa làm memory vừa làm reasoning, chủ động vứt phần thừa. MEM1-7B vượt Qwen2.5-14B-Instruct: perf 3.5x, memory 3.7x nhỏ hơn trên task 16 mục tiêu.
- **So với backend:** cùng họ với MemAgent — memory trong một episode. Backend không có tương đương và cũng không cần.
- **Học được:** ý niệm *constant memory* đáng mượn ở tầng ngân sách. Backend đã có: 1000 ký tự memory ([config.py:94](../../backend/rag_ingestion/config.py#L94)), 400 token summary, cửa sổ 6 message. Đó là đúng tinh thần. Chỗ khác là backend chọn ngân sách bằng tay còn MEM1 học nó.
- **Mức bằng chứng:** **Trung bình**. Kết quả mạnh nhưng benchmark do chính tác giả compose.

### 2.10 Memory-R1

- **Trạng thái:** ACL 2026. 169 citation. [arXiv:2508.19828](https://arxiv.org/abs/2508.19828)
- **Phương pháp:** hai agent train bằng outcome-driven RL (PPO và GRPO). *Memory Manager* học chọn ADD / UPDATE / DELETE / NOOP. *Answer Agent* lọc trước rồi mới suy luận trên entry liên quan. Chỉ **152 cặp QA** để train, generalize qua LoCoMo, MSC, LongMemEval và các cỡ model 3B-14B.
- **So với backend:** đây là paper đối chiếu sát nhất, vì nó tấn công đúng câu hỏi backend đang né: **ai quyết định thao tác memory**.
  - Thiết kế cũ: `memory/routing.py` quyết bằng luật cứng trên `key`, docstring ghi rõ lý do — model chạy cùng một câu 3 lần cho ra scope khác nhau.
  - Code hiện tại: **model quyết**, bằng cách chọn tên tool ([memory/tools.py:125-169](../../backend/rag_ingestion/memory/tools.py#L125-L169)).
  - Memory-R1: **học** policy đó bằng outcome reward, vì cả hai lựa chọn trên đều tệ theo hai kiểu khác nhau.
- **Học được:** không phải "đi train RL". Là: nếu để model quyết mà không có tín hiệu phản hồi nào, thì không có đường nào để nó tốt lên, và cũng không có cách nào biết nó đang tệ. Con số 152 mẫu cũng nói một điều thực dụng — tín hiệu ở đây rẻ hơn ta tưởng, và bước đầu tiên là *ghi lại* quyết định (đã có audit trail ở `pg_memory.py:309-328`) rồi gắn nhãn hậu nghiệm.
- **Mức bằng chứng:** **Trung bình**. ACL 2026, nhưng train set 152 mẫu quá nhỏ để tin phần generalize.

### 2.11 Hai paper phê bình, không tính vào top 10 nhưng nặng ký nhất

**How Memory Management Impacts LLM Agents** (ACL 2025, 79 citation, [arXiv:2505.16067](https://arxiv.org/abs/2505.16067)). Đo thực nghiệm hai thao tác add/delete. Phát hiện *experience-following*: input giống memory được retrieve → output giống theo. Từ đó hai failure mode:
- **error propagation** — kinh nghiệm sai tích luỹ và làm hỏng dần các lượt sau.
- **misaligned experience replay** — trajectory trông đúng nhưng replay lại vô ích hoặc gây hại.
- Đề xuất rẻ: **kết quả task tương lai là nhãn chất lượng miễn phí** cho memory đã lưu.

Chiếu vào backend: mọi memory ghi bởi tool đều mang `source = llm_extracted` và **không bao giờ bị đánh giá lại**. Không có tín hiệu nào phân biệt memory tốt với memory rác. Cột `last_used_at` tồn tại đúng cho việc này ([db/tables.py:177](../../backend/rag_ingestion/db/tables.py#L177)) và không bao giờ được ghi.

**Anatomy of Agentic Memory** (survey 2026, [arXiv:2602.19320](https://arxiv.org/pdf/2602.19320)). Bốn kết luận về nền thực nghiệm của cả lĩnh vực: benchmark bão hoà, metric lệch khỏi semantic utility, độ chính xác phụ thuộc mạnh vào backbone, và **chi phí latency/throughput của memory maintenance gần như không ai báo cáo**.

---

## 3. Cái đã tìm ra trong code

Sắp theo mức độ, không theo thứ tự đọc.

### 3.1 Đường ghi trong thiết kế đã chết, và scope quay về tay model

**Nghiêm trọng.** Không route nào gọi `write_understanding` ([deps.py:625](../../backend/rag_ingestion/api/deps.py#L625)); prompt của U chỉ còn `rewritten_query`; `understanding.memory` luôn rỗng.

Mất theo: auto-promote thread → user. Thiết kế §6.5 nói cùng một key xuất hiện ở hai thread khác nhau là bằng chứng "đây là thói quen chứ không phải yêu cầu nhất thời". Luật đó viết rồi, test rồi, và không chạy.

Và scope bây giờ do model chọn tool nào. `memory/routing.py:1-25` đã ghi bằng chứng chống lại chính điều này. Việc chuyển sang tool có lý do tốt — tiết kiệm một LLM call mỗi lượt — nhưng nó đổi luôn cả cơ chế quyết định scope, và chỗ đổi đó không thấy được ghi lại ở đâu.

Hai lựa chọn, phải chọn một:
- (a) Giữ tool, nhưng cho `route()` chạy **sau** tool: model đề xuất, `scope_for(key)` phủ quyết, `should_promote` chạy lại. Thêm ~10 dòng ở `_ContextMemoryWriter.save`.
- (b) Chấp nhận model quyết scope, xoá `memory/routing.py`, `write_understanding`, `MemoryCandidate` parsing và test tương ứng, ghi lý do vào IMPLEMENT.md.

Tệ nhất là hiện trạng: luật vẫn nằm trong repo, vẫn có test xanh, và không ảnh hưởng gì tới hành vi thật.

### 3.2 Không có forgetting, và tín hiệu để làm forgetting đang bị vứt

**Nghiêm trọng.** `last_used_at` có trong migration ([82a8b357280a:41](../../backend/migrations/versions/82a8b357280a_memory_proposals_audit_user_settings.py#L41)), trong model, trong bảng, và **không có một chỗ nào ghi vào nó**. Nó chỉ được map qua lại ở `pg_memory.py:766` và `:816`.

Hệ quả: memory sai chỉ chết khi user tự vào xoá, hoặc khi có giá trị mới đè cùng key. Đúng cái MemoryAgentBench gọi là selective forgetting, và đúng cái ACL 2025 gọi là error propagation.

Rẻ nhất để đóng: set `last_used_at` cho những memory thật sự lọt vào prompt (biết được ở `_memory_sections`, vì hàm này đã biết cái nào bị budget cắt). Có cột đó rồi thì mới nói được "memory này 60 ngày không dùng, xem lại?" — đúng đề xuất ở `2.lesson.md` §8.2 mục 2, giờ có thêm tên gọi và bằng chứng.

### 3.3 Summary viết lại toàn bộ mỗi vòng

**Nghiêm trọng.** Xem §2.4. `LLMSummarizer.summarize` nhận summary cũ và sinh summary mới hoàn toàn, trần 400 token. ACE gọi tên đúng hai thứ sẽ xảy ra: brevity bias và context collapse. Cái thứ hai nguy hiểm hơn vì không có vòng nào sai rõ ràng để bắt.

Sửa: summary thành list item có id, model chỉ phát ADD / UPDATE / KEEP. Không đổi model, không đổi số call, chỉ đổi prompt và schema.

### 3.4 Model không thể quên, chỉ có thể ghi đè

**Vừa.** Tool set chỉ có save ([memory/tools.py:171](../../backend/rag_ingestion/memory/tools.py#L171)). `MemoryOperation` enum có DELETE nhưng đường tự động không dùng. Mem0 và Memory-R1 đều xếp DELETE ngang hàng với ADD.

Sửa: thêm `forget_memory(key)`, map sang `delete_memory` đã có. Cùng lúc thêm rule vào prompt: chỉ gọi khi user nói ra một phủ định rõ ràng.

### 3.5 Overflow memory cắt theo thứ tự bảng chữ cái

**Vừa.** `_resolve()` sort theo `key` ([answering.py:74-76](../../backend/rag_ingestion/retrieval/answering.py#L74-L76)), rồi `_memory_sections` tiêu ngân sách theo đúng thứ tự đó ([answering.py:93-102](../../backend/rag_ingestion/retrieval/answering.py#L93-L102)). Nên khi vượt 1000 ký tự, cái bị rớt là những key cuối bảng chữ cái — `tone`, `response_length`, và mọi `other:t...` trở đi.

Việc *báo* số bị rớt thay vì cắt im lặng là đúng, giữ nguyên. Nhưng thứ tự cắt nên là recency hoặc scope (thread trước user), không phải alphabet. Sửa ở một dòng `sorted(...)`.

Đáng nói: với cap hiện tại (tối đa 70 row, value ≤ 2000 ký tự) thì overflow **chắc chắn xảy ra** trước khi chạm cap. Ngân sách 1000 ký tự tương đương khoảng 15-20 dòng memory. Cap cho phép 70.

### 3.6 Không đo gì về memory

**Vừa.** Có `LlmUsage` tổng ([pg_memory.py:594-621](../../backend/rag_ingestion/memory/store.py#L594-L621)) nhưng không tách được phần nào do memory. Không có regression test nào hỏi "hệ nhớ đúng không". Anatomy survey nói đây là bệnh chung của cả lĩnh vực, không riêng repo này — nhưng nó cũng có nghĩa mọi thay đổi ở §3.1-3.5 đều không chứng minh được là cải thiện.

### 3.7 Code chết cần dọn

**Nhẹ.** `understanding.py:25` `_MEMORY_CAP`, `understanding.py:54-73` parse `memory[]`, `Understanding.memory` field, `MemoryCandidate` (còn dùng bởi `_ContextMemoryWriter.save` như một struct trung gian — nên xét riêng). Phụ thuộc quyết định ở §3.1.

---

## 4. Nên làm gì, theo thứ tự

Xếp theo (giá trị ÷ công sức), không theo mức nghiêm trọng.

| # | Việc | Nguồn | Công | Đo bằng |
|---|---|---|---|---|
| 1 | Summary → item có id, update bằng delta | ACE §2.4 | Vừa | Thread 60 lượt: fact nêu ở lượt 3 còn sống ở lượt 60 không |
| 2 | Ghi `last_used_at` khi memory vào prompt | §2.11, §3.2 | Nhỏ | Cột khác NULL |
| 3 | Thêm tool `forget_memory` | Mem0 §2.2, Memory-R1 §2.10 | Nhỏ | Test: user phủ nhận fact → row bị xoá |
| 4 | Sửa thứ tự cắt overflow: recency/scope | §3.5 | Rất nhỏ | Test: memory mới nhất luôn sống sót |
| 5 | Quyết dứt điểm §3.1, rồi wire lại hoặc xoá | Memory-R1 §2.10 | Vừa | Không còn hàm memory nào không có caller |
| 6 | Eval 4 trục MemoryAgentBench | §2.8 | Lớn | Có baseline số để so |
| 7 | Rule abstention trong prompt trả lời | LongMemEval §2.3 | Nhỏ | Test: hỏi fact chưa từng nói → model nói không biết |

### Quyết định KHÔNG làm

| Không làm | Lý do |
|---|---|
| Link network kiểu A-MEM | 1 LLM call mỗi insert; closed key list đã chặn mâu thuẫn với giá gần bằng 0 (§2.1) |
| Graph memory | HippoRAG 2 là corpus memory, sai tầng (§2.6) |
| Train RL cho memory manager | Chưa có eval thì không có reward. Làm việc #6 trước (§2.10) |
| Retrieval/embedding trên memory | Cap 70 row, load-all rẻ hơn và không bao giờ miss (§2.3) |
| Procedural / workflow memory | Cần eval trước, và bản dịch sang chat RAG chưa được kiểm chứng (§2.5) |

---

## 5. Những con số không được tin

Phần này quan trọng ngang phần trên.

- **LoCoMo không đủ làm bằng chứng chính nữa.** Audit 2026: ~6.4% đáp án trong answer key sai, LLM judge chấp nhận 63% câu trả lời cố tình sai, và 56% so sánh per-category không phân biệt được với nhiễu. Độ dài hội thoại trung bình 16k-26k token, tức là model 2026 nhét thẳng vào context là xong. A-MEM, Mem0, MemoryOS, MIRIX, LightMem đều báo số chính trên đó.
- **Không so ngang số giữa các paper.** Mỗi nhóm dùng judge prompt riêng. Mem0 bảo judge "be generous"; LoCoMo gốc dùng token-overlap F1.
- **Tranh cãi Mem0 vs Zep là ví dụ mẫu.** Cả hai công bố ngay tình, và bên thứ ba không reproduce được bên nào, vì benchmark không quy định API version, category nào tính, có hỗ trợ timestamp không.
- **Citation count không phải chất lượng.** Zep có 307 citation và không qua peer review ở hội nghị nào. Ba paper trong top 10 chỉ mới accept ICLR 2026, tức là citation tích trong ~10 tháng ở giai đoạn preprint.
- **Memory-R1 train trên 152 mẫu.** Phần "generalize" cần đọc kỹ trước khi trích.
- **Con số của MemAgent và MEM1 ở miền khác.** Long-context và long-horizon agent, không phải chat memory xuyên phiên. Đừng trích cho chat.
- **Chưa xác minh sâu:** phần ablation của ACE, chi tiết prompt Reflector/Curator, và cách AWM chống workflow rác. Muốn dùng số thì phải đọc full text.

---

## 6. Đọc gì nếu chỉ đọc ba

1. **ACE** (ICLR 2026). Trực tiếp nhất với bug §3.3, và cơ chế mô tả đủ rõ để cài mà không cần code của họ.
2. **Memory-R1** (ACL 2026). Để hiểu tại sao câu hỏi "ai quyết thao tác memory" không có đáp án miễn phí.
3. **LongMemEval** (ICLR 2025). Khung 5 năng lực, dùng làm khung eval. Đọc lại kể cả đã đọc ở `2.lesson.md`.

Nếu còn thời gian: **How Memory Management Impacts LLM Agents** (ACL 2025) — ngắn, thực nghiệm, và là paper duy nhất nói thẳng chuyện memory rác làm hỏng agent theo thời gian.
