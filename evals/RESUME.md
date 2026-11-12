# Tiếp tục eval qua nhiều đợt quota

Mặc định script chỉ kiểm tra offline và in kế hoạch. `--initialize` lưu checkpoint
ban đầu, không gọi API. Chỉ `--execute` gọi API thật. Chưa chạy `--execute` cho
đến khi người thực hiện cho tiếp tục và key/quota sẵn sàng.

## Baseline: 10 case còn thiếu

PowerShell ở repo chính; dùng key đã đặt trong terminal. Không đọc `.env`, không
in key. Không copy key vào source hoặc lệnh lưu trong repo.

```powershell
$baselineRoot = 'D:\VINAI20K\CT_11112026\ReactLoopAgent-baseline'
$baselineRevision = '4a5a983d4a4bfe38249230572747da2f12ac6132'
$baselineArgs = @(
    '--source-root', $baselineRoot,
    '--revision', $baselineRevision,
    '--input', 'eval_before_incomplete.json',
    '--legacy-revision', $baselineRevision,
    '--output', 'eval_before_resumed.json'
)

# Offline: dự kiến chỉ A7, A8, H1–H8; không gọi API.
python -X utf8 evals/resume_evals.py @baselineArgs

# Chỉ chạy sau khi đã cho phép tiếp tục LLM thật:
python -X utf8 evals/resume_evals.py @baselineArgs --execute
```

Config giữ nguyên từ input: agent/judge OpenRouter,
`nvidia/nemotron-3.5-lightning:free`, max_steps 8, dataset core. CLI override khác
config input bị từ chối. Lặp **cùng lệnh** ở đợt sau: chỉ case unscored/chưa chạy
được chọn từ output hiện có. Thêm `--max-cases 1` nếu muốn giới hạn một case/đợt;
tùy chọn này không đổi config đánh giá.

`--legacy-revision` xác nhận source lịch sử theo thông tin người thực hiện. Runner
cũ không lưu revision/hash nên không thể chứng minh ngược source lúc chạy;
metadata nêu rõ giới hạn. Checkout hiện tại phải đúng commit và runtime/dataset
khớp Git. Không sửa baseline hoặc chép script sang worktree. Worker Python
`-I -B` import runner/agent/tools từ `--source-root`, không dùng agent của main.

`eval_before.json`, `eval_before_incomplete.json`, `eval_smoke_baseline.json`
giữ nguyên. Output resume lưu riêng. Output trùng input, không có provenance
hợp lệ hoặc thuộc input khác đều bị từ chối.

## Improved và bonus

Chạy sau khi baseline đầy đủ và được phép. Dùng commit chứa bản cải tiến đã kiểm
tra; ghi lại revision lần đầu và dùng **đúng revision đó** qua các đợt tiếp theo.
Runtime/data chưa commit hoặc source thay đổi bị từ chối.

```powershell
$improvedRoot = (Get-Location).Path
$improvedRevision = (git rev-parse HEAD).Trim()
$modelName = 'nvidia/nemotron-3.5-lightning:free'
$sharedArgs = @(
    '--source-root', $improvedRoot, '--revision', $improvedRevision,
    '--provider', 'openrouter', '--model', $modelName,
    '--judge-provider', 'openrouter', '--judge-model', $modelName
)

# Offline: kế hoạch 22 case, không gọi API.
python -X utf8 evals/resume_evals.py @sharedArgs --dataset core --max-steps 8 --output eval_after.json

# Khi được phép chạy thật; lặp cùng lệnh để resume:
python -X utf8 evals/resume_evals.py @sharedArgs --dataset core --max-steps 8 --output eval_after.json --execute

# Bonus: báo cáo riêng, 10 case, max_steps 16.
python -X utf8 evals/resume_evals.py @sharedArgs --dataset bonus --max-steps 16 --output eval_bonus.json --execute
```

Không đổi model nếu model không còn sẵn sàng. Execution chỉ hỗ trợ OpenRouter
với fixed `:free` model ID, từ chối model trả phí/automatic router. Thay model
hoặc config phải tạo báo cáo riêng và đánh giá lại cặp before/after phù hợp.

## Checkpoint và quota

- Giữ format `config`, `summary`, `cases` gốc. `provenance` ghi revision, SHA-256
  runtime/dataset/controller, case IDs, source root, input hash, batches và toàn
  bộ attempts. Hash source Python/JSON normalize CRLF/LF.
- `cases` là kết quả active: attempt mới thay attempt unscored trước, không chọn
  điểm cao nhất. Ghi `attempt_id`, `batch_id`, `replaces`, item đầy đủ và metadata
  lỗi provider. Scored 0 do `max_steps` giữ nguyên. Chưa chạy có `status=not_run`,
  score null: đó là tiến độ, không phải kết quả LLM giả lập.
- Tái dùng `run_case`/`grade_case`/`summarize` gốc, không sửa expected/rubric/
  fixtures/mechanical checks. Core xét đủ 22; bonus đủ 10 mới quy đổi điểm.
- Checkpoint sau từng case: file tạm + flush/fsync + atomic replace. Exclusive
  lock ngăn hai tiến trình cùng output. Scored results không bị thay bằng run
  lỗi; attempt cũ giữ trong history. Ctrl+C giữ case đã checkpoint; case đang
  chạy chưa trả kết quả có thể cần chạy lại.
- Nếu kill/mất điện có thể còn `.json.lock`. Chỉ xóa lock sau khi xác nhận không
  còn tiến trình dùng output. Không tự xóa worktree.
- Preflight GET `/api/v1/key` và `/api/v1/models` trước mỗi case: key hợp lệ,
  model tồn tại, agent hỗ trợ `tools`, quota rõ và remaining > 0. Chỉ lưu metadata
  cần thiết, không lưu key/label/response body. Budget request còn lại chia sẻ
  giữa agent/judge; chặn request kế tiếp khi budget về 0.
- Lỗi provider đầu tiên (429/402/timeout hoặc lỗi khác) mở circuit, ghi HTTP code
  từ exception chain, dừng đợt. Không tự sleep/retry LLM. Case unscored do judge
  JSON lỗi cũng dừng. Tool 429 ở observation xử lý bằng logic tools/agent gốc,
  không coi là provider 429 hoặc outage.
- Chưa có judge-only retry: chạy lại trọn case unscored và giữ attempt cũ.
  Có thể tốn thêm quota nếu agent completed nhưng judge lỗi.
- Execute exit 1 nếu dừng/còn thiếu, exit 0 khi đủ điểm tổng. Offline plan thành
  công exit 0. Source/controller thay đổi làm resume bị từ chối: giữ source và
  helper đã pin, không sửa manifest bằng tay để bỏ qua kiểm tra.

Preflight theo tài liệu chính thức: [quota/key](https://github.com/OpenRouterTeam/docs/blob/main/api_reference/limits.mdx),
[model catalogue](https://openrouter.ai/docs/api/api-reference/models/get-models).
Kiểm tra offline không request key/model/completions API.

## File nộp sau khi baseline đủ

Chỉ thay file nộp sau khi resume đủ 22 case và score khác null. Không sửa summary
để báo cáo dở trông như hoàn tất.

```powershell
$completedBaseline = Get-Content -LiteralPath 'eval_before_resumed.json' -Raw -Encoding UTF8 | ConvertFrom-Json
if ($completedBaseline.config.dataset -ne 'core' -or
    $completedBaseline.cases.Count -ne 22 -or
    $completedBaseline.summary.scored_cases -ne 22 -or
    $null -eq $completedBaseline.summary.score_0_100) {
    throw 'Baseline chưa đủ 22/22; chưa được thay file nộp.'
}
# Evidence đầu vào eval_before_incomplete.json vẫn giữ nguyên.
Copy-Item -LiteralPath 'eval_before_resumed.json' -Destination 'eval_before.json'
```
