---
title: 'Documentation index'
type: 'reference'
created: '2026-08-06'
status: 'living'
---

# Tài liệu

## Đọc theo thứ tự này

| # | File | Là gì |
|---|---|---|
| 1 | [`rag.md`](rag.md) | Tổng quan lĩnh vực RAG đa mô thức, và **kiến trúc đã chọn** cho sản phẩm này (§9b). Đọc trước tiên nếu chưa biết gì về dự án. |
| 2 | [`techstack.md`](techstack.md) | Stack đang dùng và lý do chọn. |
| 3 | [`rag-workflow/`](rag-workflow/) | Thiết kế chi tiết từng phần của pipeline, đánh số theo thứ tự xây. |

## `rag-workflow/`: thiết kế lõi, đánh số

Mỗi bước có thể có 2 file, ghép cặp bằng cùng một số:

```
N.<tên>.md       thiết kế: sẽ làm gì, vì sao chọn hướng đó, đánh đổi
N.lesson.md      nghiên cứu: các paper đã đọc để đi tới thiết kế đó
```

| Số | Thiết kế | Nghiên cứu kèm theo |
|---|---|---|
| 1 | [`1.ingestion.md`](rag-workflow/1.ingestion.md) — parse, reconcile, chunk, index | [`1.lesson.md`](rag-workflow/1.lesson.md) |
| 2 | [`2.multi-turn-memory.md`](rag-workflow/2.multi-turn-memory.md) — multi-turn, memory 4 tầng, query chain Q0-Q6 | [`2.lesson.md`](rag-workflow/2.lesson.md) |
| 3 | [`3.thread-summary.md`](rag-workflow/3.thread-summary.md) — summary dạng item list, fold pre-flight theo ngân sách, counter mức dùng. Thay §5.1 của số 2 | [`3.lesson.md`](rag-workflow/3.lesson.md) |
| 4 | [`4.memory.md`](rag-workflow/4.memory.md) — memory như đang chạy: tool tự lưu, 3 scope, cap và evict, cách chọn đưa vào prompt. Thay §3 và §6 của số 2 | [`4.lesson.md`](rag-workflow/4.lesson.md) |

Tính năng tiếp theo chạm vào pipeline lõi thì lấy số kế tiếp (`5.<tên>.md`). Không có nghiên cứu kèm thì không cần file `lesson`.

**Một ngoại lệ có chủ đích: [`lesson.md`](rag-workflow/lesson.md) không đánh số.** Nó là bản kiểm toán bắc qua nhiều bước — 10 paper agent memory đối chiếu với toàn bộ backend ngày 2026-08-15, đẻ ra cả số 3 lẫn số 4. Phần nghiên cứu chuyên sâu cho từng bước đã tách vào `N.lesson.md` tương ứng; `lesson.md` giữ phần bắc ngang và bảng "số nào không được tin".

## Tính năng lẻ: một file, ngay trong `document/`

Việc không nằm trong pipeline lõi nhưng **có ảnh hưởng tới phương pháp** thì một file phẳng ở đây, không cần đánh số:

- [`mteb-retrieval-benchmark.md`](mteb-retrieval-benchmark.md) — chạy benchmark MTEB, và chọn dataset nào để đo
- [`database/`](database/) — dữ liệu nằm ở đâu: Supabase Postgres, Qdrant Cloud, và các ràng buộc của bản managed (pooler, strict mode, payload index)

## Cái gì KHÔNG thuộc về đây

Đây là bộ lọc quan trọng nhất, vì thư mục này đã từng phình bằng đúng loại file dưới đây.

**Spec một phiên làm việc không thuộc về `document/`.** Dấu hiệu nhận biết: có checklist `- [x]`, mục "Verification commands", "Code Map", "Suggested Review Order". Loại này chết ngay khi code xong: `provider-embedding-reranking-answer.md` từng nằm ở đây, và tới lúc rút thì **8/8 đường dẫn trong Code Map của nó đã trỏ vào file không còn tồn tại**, vì nó viết trước đợt tách thư mục `backend/rag_ingestion/`. Không ai cập nhật, và cũng không ai nên phải cập nhật: chỗ của nó là `IMPLEMENT.md`.

**Cấu hình một lần cũng không thuộc về đây.** Dựng môi trường, đổi biến, chỉnh Docker: ghi vào `CHANGELOG.md`.

Câu hỏi để tự kiểm: *bỏ file này đi thì có ai thiết kế sai không?* Nếu không, nó là biên bản, không phải thiết kế.

Hai file đã rút vì không qua được bộ lọc này nằm ở [`archive/pre-trash-20260806/`](../archive/pre-trash-20260806/), không xoá hẳn. Phần thiết kế thật trong `provider-*.md` (project là phân vùng logic, ghim provider, `owner_user_id` phòng thủ nhiều lớp) đã được chuyển vào [`1.ingestion.md` §3.6](rag-workflow/1.ingestion.md) trước khi rút.

## Frontmatter: bắt buộc, ở mọi file

Quy ước này đã có sẵn trong repo, giờ áp cho tất cả:

```yaml
---
title: '...'
type: 'design' | 'research' | 'feature' | 'reference' | 'configuration' | 'orientation'
created: 'YYYY-MM-DD'
status: 'draft' | 'built' | 'partially built' | 'done' | 'living'
baseline_commit: '<sha>'        # tuỳ chọn: commit lúc chốt thiết kế
context:                        # tuỳ chọn: file cần đọc kèm
  - 'document/....md'
---
```

`status` là trường dễ mục ruỗng nhất, nên đọc nó với chút hoài nghi:

- `living` — sống cùng dự án, cập nhật liên tục (tổng quan, nghiên cứu, tra cứu)
- `draft` — đã viết thiết kế, **chưa** code
- `partially built` — code một phần; **file phải nói rõ phần nào chưa**
- `built` / `done` — đã code xong

## Ranh giới với `IMPLEMENT.md`

Đây là luật quan trọng nhất trong trang này. Hai nơi cùng nói về một tính năng, và chúng **không** được lẫn vào nhau:

| | `document/` | `IMPLEMENT.md` (ở gốc repo) |
|---|---|---|
| Trả lời | *Sẽ* làm gì, vì sao chọn hướng đó | *Đã* làm gì, ra sao |
| Viết khi | **Trước** khi code | **Sau** khi code |
| Chứa | Hình dạng dữ liệu, thuật toán, đánh đổi đã cân nhắc, tham số | File nào đã sửa, verify thế nào, khác thiết kế chỗ nào, lỗ còn hở |
| Sửa khi | Thiết kế đổi | Không bao giờ sửa mục cũ, chỉ nối thêm mục mới |

Nói ngắn: `document/` là **hợp đồng**, `IMPLEMENT.md` là **biên bản nghiệm thu**. Khi cái đã build khác thiết kế, sửa `document/` cho khớp thực tế rồi ghi lý do vào `IMPLEMENT.md`, đừng để design doc mô tả thứ không tồn tại.

`CHANGELOG.md` là trục thời gian, trả lời "hôm đó đổi gì". Không dùng nó để tra thiết kế.

## Bảng trạng thái dễ mục ruỗng nhất

`2.multi-turn-memory.md` §17 là bảng đánh dấu mục nào đã làm. **Nó từng sai và đã gây báo cáo nhầm.** Làm xong một mục thì cập nhật bảng đó ngay trong cùng lần sửa, đừng để sang lần sau.
