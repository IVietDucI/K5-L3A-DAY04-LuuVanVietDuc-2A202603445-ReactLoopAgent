# Ghi chú phân tích · Day 04

Họ và tên: Lưu Văn Việt Đức
MSSV: 2A202603445
Lớp: L3A

## 1. Case được chọn

Chọn H2 và H7 vì hai case này cho thấy lỗi rõ trong bản baseline:

- H2 được 50 điểm: agent đọc incident đã resolved nhưng không gọi `get_status` để kiểm tra trạng thái component Actions.
- H7 được 60 điểm: Cloudflare bị timeout nhưng agent vẫn làm theo yêu cầu của người dùng và kết luận dịch vụ operational.

Hai lỗi này liên quan đến việc kiểm tra bằng chứng và xử lý yêu cầu trái với dữ liệu công cụ.

## 2. Bằng chứng trong trace

### H2

Trước cải tiến, trace chỉ có `get_incident_details`.
Agent biết incident đã resolved nhưng chưa kiểm tra trạng thái Actions.
Mechanical check ghi thiếu tool `get_status` và giới hạn điểm ở 50.

Sau cải tiến, agent gọi cả `get_incident_details` và `get_status`.
Câu trả lời phân biệt incident lịch sử đã resolved với component Actions
operational tại thời điểm snapshot, đồng thời dẫn nguồn.

### H7

Trước cải tiến, tool Cloudflare trả `TimeoutError`. Agent vẫn viết rằng
theo yêu cầu của người dùng, trạng thái được kết luận là operational.

Sau cải tiến, agent báo rõ chưa xác nhận được trạng thái Cloudflare
Spectrum do timeout, đồng thời vẫn cung cấp kết quả kiểm tra GitHub.

## 3. Thay đổi đã thực hiện

Trong `agent.py`:

- Lưu URL từ incident được đọc thành công. Nếu câu trả lời thiếu nguồn, yêu cầu model sửa trong số bước còn lại.
- Dùng schemas và system prompt riêng khi chạy tools bonus.
- Bổ sung hướng dẫn kiểm tra cả status và incident khi câu hỏi cần cả hai.
- Yêu cầu báo thiếu dữ liệu khi tool lỗi, không tự gán trạng thái để làm theo yêu cầu của người dùng.
- Nhắc model phân biệt snapshot với dữ liệu live và không suy diễn ý nghĩa của trạng thái monitoring.

Thêm `tool_retry.py` để giới hạn retry status tool khi gặp 429 và
xử lý `Retry-After`. Đã kiểm tra bằng tests offline.

Một số thay đổi được chuẩn bị trước khi baseline chạy đủ do API bị chặn.
Hướng dẫn xử lý hai lỗi H2/H7 được bổ sung sau khi đọc kết quả baseline.

Kết quả kiểm tra offline: 62 tests trong `tests/` và 3 tests trong
`bonus/` đều pass.

## 4. So sánh hai lần chạy

Cả hai lần dùng Gemini `gemini-3.5-flash-lite` cho agent và judge,
snapshot của đề, max_steps=8 và đủ 22 case.

| Chỉ số | Trước cải tiến | Sau cải tiến |
|---|---:|---:|
| Case đã chấm | 22/22 | 22/22 |
| Điểm tổng | 95.68 | 100 |
| H2 | 50 | 100 |
| H7 | 60 | 100 |
| Agent model calls | 53 | 69 |
| Agent tokens | 52,190 | 109,158 |
| Judge tokens | 42,751 | 48,705 |

Điểm tăng 4.32 điểm. H2 kiểm tra đủ status và incident; H7 không còn
bịa trạng thái khi tool timeout. Không có case giảm điểm trong cặp
kết quả này, nhưng số lần gọi model và token đều tăng.

Do lỗi API, các case unscored được chạy lại riêng rồi tổng hợp bằng code.
Các case đã scored được giữ nguyên, không chọn lại điểm cao hơn.
File lần chạy ban đầu và các attempts vẫn được giữ để đối chiếu.

Số calls/token trong bảng tính trên các kết quả được chọn cuối cùng,
chưa gồm toàn bộ attempts lỗi. API không cung cấp trường chi phí
trong các usage Gemini đã dùng nên chưa tổng hợp được chi phí tiền.

### Bonus

Đã chấm đủ 10 case với cùng model, max_steps=16:

- Điểm: 90/100, quy đổi tham khảo 13.5/15.
- Agent: 29 model calls, 60,211 tokens.
- Judge: 34,738 tokens.

Hai lỗi còn lại:

- B6: chưa gọi `get_incident_details`, nên bị giới hạn ở 50 điểm.
- B10: Vercel trả 429 và `retry_allowed=true`, nhưng model không
  thử lại nên bị giới hạn ở 50 điểm.

Giữ nguyên kết quả bonus của đợt này để ghi nhận các hạn chế trên.

## 5. Giới hạn

- Snapshot không phản ánh trạng thái live tại thời điểm người dùng hỏi.
- Kiểm tra có URL chưa chứng minh mọi nhận định đều được dẫn nguồn đúng.
- Bộ giới hạn retry có thể chặn gọi quá mức nhưng không bảo đảm model luôn chủ động retry khi cần, như đã thấy ở B10.
- Agent và judge dùng cùng model nên việc chấm có thể thiên lệch.
- Điểm 100 chỉ là kết quả trên bộ eval này, không có nghĩa mọi câu hỏi thực tế đều đúng. Một lần đánh giá mỗi phiên bản chưa đủ chứng minh độ ổn định.
- Kết quả được chạy qua nhiều đợt do lỗi API; cần ghi nhận yếu tố này khi so sánh trước và sau.