# Ghi chú phân tích · Day 04

Họ và tên: Lưu Văn Việt Đức

MSSV: 2A202603445

Cập nhật offline: 11/11/2026 (giờ Việt Nam).

## 1. Case được chọn

Chọn **E3** trong `eval_before.json` thực tế, baseline `4a5a983`:

> Cloudflare Spectrum có incident ở đâu? Có đủ căn cứ nói incident đó gây lỗi dịch vụ của nhóm ở Việt Nam không?

Expected của đề: incident Spectrum tại Phoenix đang `monitoring` theo snapshot;
chưa đủ căn cứ kết luận gây lỗi dịch vụ của nhóm Việt Nam. Không sửa expected.

Agent xác định đúng incident, dẫn đúng URL và trả lời chưa đủ bằng chứng về
quan hệ nhân quả. Tuy nhiên câu trả lời còn có diễn giải vượt dữ liệu:

> Incident status: `monitoring` (investigation completed on 2026-11-07)

> No new updates have been added since 7 Nov, suggesting the issue is either contained or still under investigation.

Điểm judge **90/100**: factuality 35, completeness 23, evidence 18, boundaries 14.
`grade.reason` xác nhận phần Phoenix/không đủ căn cứ về Việt Nam phù hợp expected.
`grade.improvement` đề nghị làm rõ vùng ảnh hưởng và có dữ liệu traffic/routing
nếu cần. Judge **không giải thích rõ từng điểm bị trừ**; không suy diễn rằng lỗi
do tôi đối chiếu dưới đây chính là nguyên nhân chính xác của điểm 90.

## 2. Bằng chứng trong trace

E3 gọi `get_status(provider="cloudflare", component="Spectrum")` ở step 1,
`get_incident_details` ở step 2, trả lời ở step 3. Mechanical violations rỗng,
score cap 100, 2 tool calls ở 2 bước. Trích result thật của step 2 (rút gọn trường):

```json
{
  "captured_at": "2026-11-09T03:50:47.638054+00:00",
  "id": "c60k3g4vsoc1",
  "status": "monitoring",
  "updates_total": 2,
  "updates": [
    {
      "status": "monitoring",
      "created_at": "2026-11-07T02:51:59.805Z",
      "body": "We are continuing to monitor for any further issues.",
      "truncated": false
    },
    {
      "status": "investigating",
      "created_at": "2026-11-07T00:59:31.548Z",
      "body": "Cloudflare is investigating issues with Spectrum in Phoenix. This may be impacting the availability of existing Spectrum applications. We are working to understand the full impact and mitigate this problem. More updates to follow shortly.",
      "truncated": false
    }
  ],
  "url": "https://www.cloudflarestatus.com/incidents/c60k3g4vsoc1"
}
```

Trace không nói investigation đã hoàn tất hoặc sự cố đã được khống chế. Không có
update mới trong snapshot cũng không tự chứng minh các kết luận đó. Câu trả lời
còn nói “all updates reference” Phoenix, trong khi update monitoring chỉ nói đang
theo dõi. Đây là vấn đề diễn giải nguồn, không phải thiếu URL hoặc không gọi tool.

Mười case A7, A8, H1–H8 có `status=error`, step 1 chỉ có `provider_error`,
`type=RuntimeError`; chưa có bằng chứng hành vi tool/answer ở các case này. Theo
lịch sử phiên làm việc do người thực hiện cung cấp, quota kiểm tra sau đợt đó là
remaining=0. Trace cũ không lưu HTTP status nên **không thể xác nhận từng lỗi cũ
đều là HTTP 429**. Không gán lỗi quan sát trong smoke/E3 cho các case chưa chạy.

## 3. Thay đổi đã thực hiện

- Các commit đã có: validation URL incident (`1758389`), dùng schema/system prompt
  riêng của tools (`c522b51`), retry status tools (`024e144`), test pagination
  (`883d46f`). Chúng được chuẩn bị **trước khi full baseline hoàn tất** vì hết
  quota; không tuyên bố bắt nguồn từ trace chưa quan sát.
- Bổ sung `EVIDENCE_GUIDANCE` trong `agent.py` sau khi đọc E3: diễn đạt theo
  `captured_at`, giữ đúng ý nghĩa `monitoring`, không suy ra đã hoàn tất/khống chế
  hoặc diễn biến hiện tại chỉ vì không thấy update mới. Áp dụng core và bonus;
  không hard-code case ID, incident ID hay expected vào agent.
- `evals/resume_evals.py` dùng `run_case`, `grade_case` qua runner gốc và
  `summarize` gốc. Worker Python cô lập import agent từ checkout pin revision,
  kiểm tra hash runtime/dataset/controller. Không sửa baseline, fixtures, cases,
  scenarios, rubric hay mechanical grading.
- Checkpoint sau từng case, giữ attempt cũ và `replaces`; chỉ chạy unscored/chưa
  chạy, không chọn điểm cao nhất. `max_steps` được chấm 0 vẫn giữ là scored.
  File input giữ nguyên. Điểm tổng chỉ có khi đủ toàn bộ dataset.
- Khi được phép chạy thật, preflight OpenRouter kiểm tra key/model/tools/quota
  trước mỗi case. Budget request dùng chung agent/judge. Dừng ngay khi provider
  lỗi hoặc case unscored; không tự retry LLM 429. Lớp hạ tầng này nằm ngoài
  baseline, riêng với retry status tool. Không đổi model/key/tài khoản.
- Tests offline: skip scored 0, config/source/data/controller mismatch, import
  qua subprocess, checkpoint khi ngắt, giữ attempts, quota/circuit breaker,
  tổng điểm null khi thiếu case. Hiện **62 tests trong `tests/`, 3 trong `bonus/`
  đều pass**; `git diff --check` sạch.

Lệnh PowerShell ở [evals/RESUME.md](evals/RESUME.md). Checkpoint khởi tạo offline
chỉ sao chép kết quả thật đã có và thêm provenance; không phải lần eval mới.

## 4. So sánh hai lần chạy

Chưa có `eval_after.json` hoặc `eval_bonus.json`; chưa gọi lại LLM trong đợt làm
offline này. `eval_before_incomplete.json` đã xác nhận giống byte-for-byte với
`eval_before.json`; giữ nguyên cả hai làm bằng chứng.

| Chỉ số | Baseline hiện có, chưa hoàn tất | Improved |
|---|---|---|
| Case đã chấm | 12/22, còn 10 unscored | chưa có |
| Điểm tổng | `null` | chưa có |
| Trung bình phần đã chấm | 99.17, **không phải điểm tổng** | chưa có |
| E3 | 90/100 | chưa có |
| Agent model calls | 35 model events thành công + 10 provider_error ở step 1 | chưa có |
| Agent tokens API trả về | 52,644 từ 35 usage entries | chưa có |
| Judge calls có usage | 12 | chưa có |
| Judge tokens API trả về | 46,384 | chưa có |
| Cost API trả về | 0 trong usage entries đã lưu của agent và judge | chưa có |

Riêng E3: agent 3 calls / 4,126 tokens; judge 1 call / 5,394 tokens; cost API
báo 0. Không có usage/cost cho 10 lần gọi agent lỗi, nên không suy ra chi phí
không được API báo. Sau khi đủ eval phải phân biệt chi phí toàn bộ attempts
qua nhiều đợt với chi phí các kết quả active cuối.

Chưa có căn cứ kết luận prompt mới đã sửa E3, tăng điểm hay gây regression.
CP4 còn thiếu 10 case; CP6 và bonus chờ cho phép chạy thật, quota/key/model sẵn
sàng. Unit tests không thay thế eval phải nộp.

## 5. Giới hạn

- Validation chỉ yêu cầu ít nhất một URL incident đã đọc, chưa xác minh từng
  claim/provider. Prompt mới không bảo đảm model luôn tuân theo.
- Snapshot không phải live. Tools có cửa sổ tìm kiếm và giới hạn output/timeline;
  khi còn `next_offset`/truncated không nói đã xem hết. Tests bonus xác nhận
  registry 5 APIs và hành vi giả lập; chưa kiểm tra live 5 APIs hoặc đủ eval bonus.
- Judge cùng model với agent có thể thiên lệch. Điểm 90 không tự chứng minh lỗi
  nào bị trừ; điểm 100 không bảo đảm mọi claim đúng. Một lần chạy mỗi phiên bản
  không chứng minh độ ổn định.
- Report cũ không có hash source lúc chạy. Legacy revision ghi theo thông tin
  người thực hiện cung cấp; công cụ xác minh checkout hiện tại, không thể chứng
  minh ngược lịch sử. Đợt resume ghi revision/hash/attempt rõ ràng.
- Resume hiện chạy lại trọn case unscored, kể cả khi chỉ judge lỗi; chưa có
  judge-only retry. Không chạy lại case đã scored, kể cả scored 0.
- Tên repo chưa đúng mẫu `K5-L3A-DAY04-...` hoặc `K5-L3B-DAY04-...`; cần xác nhận
  lớp trước khi đổi tên. Chưa đổi remote, chưa nộp LMS. Vercel bonus là nguồn
  status, không yêu cầu deploy.
- File nộp cuối: `agent.py`, `tool_retry.py`, `eval_before.json` đủ 22/22,
  `eval_after.json` đủ 22/22, `NOTES.md`; nếu bonus thêm `eval_bonus.json` đủ 10/10.
  Giữ evidence lần chạy dở và file gốc của đề.
