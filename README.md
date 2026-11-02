# Paotwin: bản sao số của một bàn thử

Bản demo cho tính năng số hoá bản vẽ máy: một mô hình 3D của máy, có vùng
**đỏ lên đúng chỗ** khi cảm biến báo lỗi. Người bảo trì nhìn hình là biết phải
mở chỗ nào, không phải đọc bảng số.

Chưa có máy thật để chạm vào, nên máy thử là chính chiếc laptop này, thêm hai
thiết bị ảo kiểu máy cũ phải gắn thêm cảm biến (như PoC M64):

| Bộ phận | Tín hiệu | Nguồn |
| --- | --- | --- |
| GPU | nhiệt độ, tải, VRAM, công suất | thật, `nvidia-smi` |
| CPU | tải | thật, Windows |
| CPU | nhiệt độ | chưa có cảm biến (Windows không cho đọc) |
| RAM | phần trăm đang dùng | thật, Windows |
| Ổ dự án (ổ E: cắm ngoài) | phần trăm còn trống | thật |
| Pin | mức pin, đang cắm sạc hay chạy pin | thật, Windows |
| Wi-Fi | độ mạnh sóng | thật, `netsh wlan` |
| Quạt | tốc độ | chưa có cảm biến |
| Động cơ ảo | dòng 3 pha, lệch pha | mô phỏng, đọc qua Modbus TCP |
| Đèn tháp ảo | Chạy / Chờ / Lỗi | mô phỏng, đọc qua Modbus TCP |
| Gateway ảo | còn kết nối hay không | MQTT Last Will |

Không cần tải gì: broker MQTT 3.1.1, Modbus TCP và hình 3D (WebGL) được viết
bằng thư viện có sẵn của Python và trình duyệt. Chỉ cần Python 3.10 trở lên.

## Chạy

1. Bấm đúp `CHAY-DEMO.cmd`.
2. Trình duyệt mở hai trang:
   - **Bản sao 3D**: http://127.0.0.1:8791/ (chỉ để xem, không ghi gì xuống thiết bị)
   - **Bảng mô phỏng**: http://127.0.0.1:8792/ (tạo sự cố cho thiết bị ảo)
   Hai trang có nút mở qua lại ("Mở bảng tạo sự cố", "Mở bản sao 3D"). Đặt
   chúng cạnh nhau: bấm ở bảng mô phỏng, xem vùng đổi màu ở bản sao 3D.
   Nếu Windows hỏi "Chọn ứng dụng để mở", chọn một trình duyệt, hoặc tự mở hai
   địa chỉ trên.
3. Muốn dừng: đóng cửa sổ đen, hoặc bấm Ctrl+C trong đó.

Kiểm tra tự động: bấm đúp `KIEM-TRA.cmd` (khoảng 15 giây).

## Thử các sự cố

Trên **Bảng mô phỏng**:

| Bấm | Bản sao 3D phải hiện |
| --- | --- |
| Mất pha → Pha B | Động cơ đỏ (lệch pha), đèn tháp đỏ |
| Tải kéo lên 110 % | Động cơ vàng (cảnh báo dòng) |
| Tải 125 % | Động cơ đỏ |
| Rút cáp Modbus | Động cơ và đèn tháp sọc xám (mất tín hiệu), kèm lý do |
| Cắt mạng | Gần như ngay lập tức, mọi vùng sọc xám: broker phát Last Will của gateway |
| Mạng treo | Sau khoảng 6 s mọi vùng sọc xám vì dữ liệu cũ; sau khoảng 10 đến 15 s, khi gateway hoặc broker nhận ra đường mạng im lặng, vùng gateway mới báo mất kết nối |
| Chậm 8 s | Số đo vẫn tới đủ nhưng trễ 8 s: mọi vùng sọc xám, lý do ghi "Số đo đến trễ"; gateway vẫn báo đang kết nối. Trả về Bình thường thì khoảng 8 s sau mới hết, vì phần đang đọng phải đi hết |

Khi có vùng mới chuyển sang Lỗi mà 15 s qua không ai chạm vào trang, hình tự
xoay tới vùng đó và mở phần chi tiết.

Trên laptop thật: chạy một việc nặng GPU, rút sạc, hay tắt Wi-Fi để xem các
vùng tương ứng đổi màu.

## Quy tắc hiển thị

Mỗi vùng lấy trạng thái **xấu nhất** trong các tín hiệu của nó, theo thứ tự:

1. **Lỗi** (đỏ, nhấp nháy nhẹ): vượt ngưỡng lỗi.
2. **Mất tín hiệu** (sọc xám): không có số mới và đáng tin. Đứng trên Cảnh báo
   vì một chỗ không nhìn thấy có thể đang ở bất kỳ trạng thái nào.
3. **Cảnh báo** (vàng): vượt ngưỡng cảnh báo.
4. **Bình thường** (giữ màu gốc của bộ phận, không tô): có số mới, nằm trong ngưỡng.
5. **Chưa có cảm biến** (trong suốt, chỉ có viền): chưa có gì đo chỗ này.

Vùng lỗi, cảnh báo hay mất tín hiệu nằm khuất sau bộ phận khác vẫn hiện viền
xuyên qua vật che, nên không phải xoay hình mới thấy.

Mất tín hiệu **không bao giờ** được hiện là bình thường. Bản sao coi là mất khi:
chưa nhận được số nào, quá 6 s không có số mới, gateway báo không đọc được,
gateway mất kết nối (Last Will), máy chủ mất kết nối broker, số đo tới nơi đã
cũ quá 6 s tính từ lúc đo (đến trễ), hoặc chính trang web không nhận được cập
nhật từ máy chủ quá 5 s.

## Trạng thái máy

Ngoài màu từng vùng, khung **Máy** ghi trạng thái cả máy ảo theo đúng bộ từ của
PoC M64: Gia công, Chờ, Tắt, Lỗi, lấy từ đèn tháp. Khi mất tín hiệu đèn, máy
ghi **"Không rõ: tắt máy hoặc mất tín hiệu"**, vì nếu gateway lấy điện từ tủ
máy thì hai việc này trông giống hệt nhau. Khung cũng ghi khi đèn và dòng động
cơ không khớp nhau, và tổng thời gian ở từng trạng thái từ lúc mở máy chủ. Chưa
chia theo ca vì chưa có giờ ca.

## Chi tiết từng vùng

Bấm vào một vùng sẽ thấy:

- **Việc cần kiểm tra**: hướng dẫn mẫu cho bản demo, chưa được tổ bảo trì duyệt
  (sửa trong `mo-hinh/ban-thu.json`, mục `kiem_tra` của từng vùng).
- **Từng tín hiệu**: số, trạng thái, ngưỡng và nguồn của ngưỡng.
- **Biểu đồ diễn biến** tối đa 30 phút, có đường ngưỡng. Chỗ mất số thì đường
  bị ngắt, không nối liền cho đẹp.

Số đo thô chỉ giữ trong bộ nhớ (khoảng 30 phút). Mỗi phút, máy chủ ghi một dòng
cho mỗi tín hiệu (nhỏ nhất, trung bình, lớn nhất) vào
`nhat-ky/so-do-NĂMTHÁNGNGÀY.csv`, mỗi ngày khoảng 1 MB, để không ghi liên tục
lên ổ cắm ngoài.

Mỗi trạng thái có chữ và ký hiệu đi kèm, không chỉ dựa vào màu.

## Đưa bản vẽ vào (DXF)

Hình 3D có hai nguồn:

- **Mô hình làm tay** (`mo-hinh/ban-thu.json`), mức *Tham khảo*. Đây là mặc định.
- **Bản vẽ DXF**, nhập vào sổ bản vẽ. Mới nhập thì ở mức *Candidate* (chưa đối
  chiếu với máy thật). Khi có người đối chiếu với máy và ghi tên mình, bản vẽ
  lên mức *Verified*.

Cách làm:

1. Kéo thả tệp `.dxf` lên `NHAP-BAN-VE.cmd`. Bấm đúp mà không kéo gì thì nó
   nhập bản vẽ mẫu `ban-ve\mau\BT-001-A.dxf`.
2. Đóng demo đang chạy (nếu có) rồi bấm `CHAY-DEMO.cmd`. Khung tên góc hình ghi
   số bản vẽ, revision, mức, tệp gốc.
3. `SO-BAN-VE.cmd`: xem các bộ bản vẽ, ghi xác minh (tên người, đã đối chiếu
   gì), hoặc chọn bộ cho bản sao dùng (`tham-khao` để quay về mô hình làm tay).

Quy ước vẽ, theo đúng nghĩa sẵn có của DXF (vẽ 2.5D trong AutoCAD):

| Trong bản vẽ | Thành |
| --- | --- |
| Đường bao khép kín (POLYLINE, LWPOLYLINE) trên mặt bằng, có cao độ (elevation) và độ dày (thickness) | Khối lăng trụ từ cao độ đó lên hết độ dày |
| Đường tròn (CIRCLE) có độ dày | Khối trụ đứng |
| Lớp `VUNG-<MÃ>`, ví dụ `VUNG-GPU`, `VUNG-DONG_CO` | Khối thuộc vùng đó |
| Lớp `KHUNG-NEN`, `KHUNG-VO`, `KHUNG-TRONG` | Mặt bàn, vỏ, chi tiết bên trong không gắn cảm biến |
| Chữ trên lớp `KHUNG-TEN`: `SO BAN VE: …`, `REV: …`, `TEN: …` | Khung tên |
| Màu nét 1 (đỏ), 2 (vàng), 3 (xanh) ở vùng đèn tháp | Đèn tương ứng |

Đơn vị là mm. Tên, mô tả và việc cần kiểm tra của từng vùng **không** nằm
trong bản vẽ mà nằm trong danh mục vùng (`mo-hinh/ban-thu.json`), nên đổi bản
vẽ mới không làm mất cảnh báo. Bản vẽ phải có đủ mọi vùng của danh mục và
không có vùng lạ, nếu không thì bị từ chối kèm tên lớp thiếu hoặc thừa.

Những gì không dựng được thì không đoán: đường bao không có độ dày, không khép
kín, có cung tròn, mặt phẳng nghiêng, khối tham chiếu (INSERT)… đều bị bỏ qua
và được liệt kê khi nhập. Lớp khác (kích thước, ghi chú) không được dùng.

Sổ bản vẽ (`ban-ve/so-ban-ve.json`) giữ bản sao của mỗi tệp đã nhập
(`ban-ve/tep/`), mã SHA-256, mức, quyền dùng và **lịch sử không ghi đè**: nhập,
chọn dùng, xác minh, ai làm, lúc nào. Nhập lại cùng số bản vẽ, cùng revision
nhưng nội dung khác thì bị từ chối: bản vẽ sửa phải lên revision mới, bản cũ
vẫn giữ.

Bản vẽ mẫu `BT-001-A.dxf` do Claude dựng từ mô hình làm tay để thử đường đi,
không phải bản vẽ của hãng. Màn hình laptop và động cơ nằm ngang được vẽ thành
khối đứng, vì mặt bằng 2.5D không diễn tả được hình nghiêng. Đó là giới hạn
của đường "2D → khối" trong kế hoạch.

### Bản vẽ 3D (STEP)

Đọc STEP cần thư viện OpenCASCADE: gói `cadquery-ocp-novtk` bản 8.0.1.1.0
(khoảng 47,5 MB; giấy phép Apache-2.0, lõi OpenCASCADE theo LGPL 2.1).

1. Bấm đúp `CAI-DAT-STEP.cmd` (một lần, cần Internet). Gói được cài vào
   `.venv` riêng của dự án, không đụng tới Python dùng chung. Xong nó tự chạy
   bài kiểm tra STEP: tạo một tệp STEP có cụm lắp ráp từ mô hình làm tay, đọc
   lại, so vị trí; kết quả ghi ở `nhat-ky\tu-kiem-tra-step.json`.
2. Kéo thả tệp `.step` / `.stp` lên `NHAP-BAN-VE.cmd`, nhập số bản vẽ và
   revision (STEP không có khung tên đọc được).

Quy ước tên giống lớp DXF: chi tiết hoặc cụm tên `VUNG-<MÃ>` hay
`VUNG-<MÃ>-<gì cũng được>` thuộc vùng đó, kể cả mọi thứ bên trong cụm;
`KHUNG-NEN`, `KHUNG-VO`, `KHUNG-TRONG` là khung; ở vùng đèn tháp, tên kết thúc
`-XANH`, `-VANG`, `-DO` là đèn tương ứng. Bản vẽ của hãng dùng tên riêng thì
không cần sửa bản vẽ: viết tệp ánh xạ JSON `{"tên chi tiết trong STEP": "mã vùng"}`
và đưa vào lúc nhập. Thiếu vùng nào, việc nhập bị từ chối và in ra danh sách
tên chi tiết chưa gán để viết ánh xạ.

OpenCASCADE đổi đơn vị về mm và chia bề mặt thành tam giác với độ mịn mặc định
0,2 % đường chéo của máy (ít nhất 0,1 mm); quá 400 000 tam giác thì bị từ chối,
nhập lại với `--do-min` lớn hơn. Đường nét trên hình là cạnh thật của mô hình
CAD, không phải cạnh tam giác.

## Về các con số

- **Ngưỡng nhiệt độ GPU** lấy từ chính card (`nvidia-smi -q -d TEMPERATURE`):
  lỗi từ *GPU Slowdown Temp* (card tự giảm xung), cảnh báo từ *GPU Max
  Operating Temp* nếu mức này thấp hơn. RTX 3050 Laptop của máy thử báo
  Max Operating 105, Slowdown 97, Shutdown 100 (°C), tức Max Operating cao hơn
  Slowdown, nên vùng GPU chỉ có mức lỗi (97 °C), không có mức cảnh báo. Bản sao
  không tự đặt số thay. Nếu card không báo gì, bản sao chỉ hiện số và ghi rõ là
  không có ngưỡng.
- **Mọi ngưỡng khác** là số tạm, ghi **"DEMO, chưa duyệt"** trên giao diện và
  trong `cau-hinh/tin-hieu.json`. Không dùng cho máy thật.
- **6 s mất tín hiệu** là giả định demo (3 lần chu kỳ gửi 2 s), chưa duyệt.
- **Độ trễ** là số đo được, hiện ở cuối cột phải: từ lúc gateway đọc tới lúc
  máy chủ nhận. Không phải con số cam kết.
- **Mô hình 3D** là khối minh hoạ, mức bản vẽ *Tham khảo*: vị trí linh kiện
  bên trong laptop là ước lượng, chưa đo theo máy thật.

## Cấu trúc

```
CHAY-DEMO.cmd          chạy toàn bộ
KIEM-TRA.cmd           chạy bộ kiểm tra
NHAP-BAN-VE.cmd        nhập một bản vẽ DXF hoặc STEP (kéo thả tệp lên)
CAI-DAT-STEP.cmd       cài thư viện đọc STEP vào .venv (một lần)
SO-BAN-VE.cmd          xem sổ bản vẽ, xác minh, chọn bộ bản vẽ
ban-ve/mau/            bản vẽ mẫu BT-001-A.dxf
ban-ve/so-ban-ve.json  sổ bản vẽ và lịch sử (có sau lần nhập đầu)
cau-hinh/mang.json     cổng và chu kỳ gửi
cau-hinh/tin-hieu.json tín hiệu, vùng, ngưỡng
mo-hinh/ban-thu.json   mô hình khối 3D (mm)
paotwin/mqtt.py        broker và client MQTT 3.1.1
paotwin/modbus.py      Modbus TCP (chỉ đọc: mã 02, 03, 04)
paotwin/sensors.py     đọc cảm biến thật của máy
paotwin/gateway.py     gateway ảo: đọc rồi gửi lên MQTT
paotwin/sim.py         thiết bị ảo, đường mạng ảo, bảng mô phỏng
paotwin/engine.py      tính trạng thái từng tín hiệu, từng vùng và cả máy
paotwin/dxf.py         đọc bản vẽ DXF, dựng khối
paotwin/step.py        đọc bản vẽ STEP (OpenCASCADE), gán vùng theo tên
paotwin/ban_ve.py      sổ bản vẽ: nhập, xác minh, chọn dùng
paotwin/server.py      máy chủ bản sao (chỉ có GET, đẩy cập nhật qua SSE)
web/                   trang bản sao 3D và trang mô phỏng
tests/                 kiểm tra tự động
nhat-ky/               nhật ký mỗi lần chạy, su-kien.jsonl là các lần đổi trạng thái
```

Luồng dữ liệu:

```
cảm biến laptop ─┐
động cơ ảo ──────┼─ Modbus TCP ─ gateway ảo ─ đường mạng ảo ─ broker MQTT ─ máy chủ bản sao ─ trình duyệt
đèn tháp ảo ─────┘                                                              (SSE, chỉ đọc)
```

## Cổng

Tất cả chỉ mở trên `127.0.0.1`, nên chỉ máy này thấy:
broker 1884, đường mạng ảo 1885, Modbus 5021, bản sao 8791, mô phỏng 8792.
Đổi trong `cau-hinh/mang.json` nếu trùng.

Muốn xem bản sao từ máy khác trong phòng (máy tính bảng, điện thoại): đổi
`web.host` thành `0.0.0.0`. Khi đó ai cùng mạng cũng xem được thông số máy,
vì trang chưa có đăng nhập. Chỉ làm trong mạng tin cậy.

## Giới hạn đã biết

- Broker chỉ hỗ trợ QoS 0 và 1, không lưu phiên, không TLS, không tài khoản.
  Bản thật dùng broker đầy đủ (Mosquitto, EMQX) có TLS và tài khoản.
- Gateway không giữ lại số khi mất mạng; khoảng đó hiện là mất tín hiệu.
- Bảng địa chỉ Modbus của thiết bị ảo là của riêng bộ mô phỏng. Thiết bị thật
  (ví dụ kẹp dòng HD-273I ở M64) lấy bảng địa chỉ từ tài liệu của hãng.
- Trên Windows 11 bản mới, đọc Wi-Fi có thể cần bật quyền Vị trí cho ứng dụng
  máy tính; nếu chưa bật, vùng Wi-Fi hiện mất tín hiệu và ghi rõ lý do.
