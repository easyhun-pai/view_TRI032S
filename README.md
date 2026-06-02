# TRI032S Web Viewer

사내 네트워크(게이트웨이 192.168.33.1)에서 Lucid Vision **TRI032S** 카메라를
브라우저로 보고, 녹화·설정 조절·스냅샷까지 할 수 있는 웹 인터페이스.

- **백엔드**: FastAPI + arena_api (단일 프로세스가 카메라 소유 → 다중 뷰어로 fan-out)
- **영상**: WebRTC (aiortc, H.264) — 저지연, 1~3명 동시 시청 대상
- **기능**: 라이브 보기 / 녹화 시작·정지 / 노출·게인·프레임레이트 조절 / 스냅샷

> TRI032S는 GigE Vision 머신비전 카메라라 RTSP/ONVIF로 직접 못 봅니다.
> 프레임을 arena_api로 grab → BGR 변환 → WebRTC로 재송출하는 구조입니다.
> 카메라 master 연결은 1개 프로세스만 가능 → **ArenaView를 닫은 상태**에서 서버를 실행하세요.

## 구성

| 파일 | 역할 |
|------|------|
| `server.py` | FastAPI 진입점 (WebRTC 시그널링, 설정/녹화/스냅샷 API, 정적 페이지) |
| `camera.py` | 카메라 소유 스레드: arena_api로 grab, 최신 프레임 공유, 설정 적용. **카메라가 없으면 시뮬레이션 영상 송출** |
| `recorder.py` | grab 스레드가 먹여주는 프레임으로 mp4 녹화 |
| `webrtc.py` | 최신 프레임을 WebRTC 트랙으로 제공 (스트림용 다운스케일) |
| `config.py` | 모든 설정값 (해상도/프레임레이트/포트/스트림 폭 등) |
| `static/index.html` | 뷰어 + 제어 패널 (순수 HTML/JS) |

## 설치

```powershell
# 1) 가상환경 (이미 .venv가 있으면 생략)
py -3.11 -m venv .venv

# 2) arena_api (SDK 동봉 휠 — 다운로드 불필요)
.\.venv\Scripts\python.exe -m pip install `
  "C:\ProgramData\Lucid Vision Labs\Examples\Python Source Code Examples\wheel\arena_api-2.9.0-py3-none-any.whl"

# 3) 웹 의존성
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 실행

```powershell
.\.venv\Scripts\python.exe -m uvicorn server:app --host 0.0.0.0 --port 8000
```

- 같은 PC: <http://localhost:8000>
- 사내 다른 PC: `http://192.168.33.54:8000` (이 서버 PC의 이더넷 2 IP)
- **Windows 방화벽**에서 8000 포트 인바운드 허용이 필요할 수 있습니다.

## 시뮬레이션 모드

카메라가 연결돼 있지 않으면 자동으로 **합성 테스트 영상**(SIMULATION 표시)을 송출합니다.
지금 카메라 없이도 웹/WebRTC/녹화/스냅샷 동작을 그대로 확인할 수 있고,
나중에 **카메라를 연결한 뒤 서버를 재시작하면** 코드 수정 없이 실제 카메라로 전환됩니다.
시뮬레이션 모드에서는 카메라 설정(노출/게인) 조절만 비활성화됩니다.

## 특정 카메라 지정 (선택)

여러 대가 잡히거나 특정 장치를 고정하려면 `CAM_INFO.json`을 만드세요(gitignore됨):

```json
{ "serial": "241400123" }
```

또는 `{ "ip": "192.168.33.60" }`. 없으면 첫 번째로 발견된 카메라를 사용합니다.

## 주요 튜닝값 (`config.py`)

- `STREAM_MAX_WIDTH` — 브라우저로 보내기 전 다운스케일 폭 (지연/대역폭 ↓)
- `ACQUISITION_FRAME_RATE` — GigE 대역폭 관리를 위한 프레임레이트 상한
- `PREFERRED_PIXEL_FORMATS` — BGR8 → RGB8 → Mono8 순으로 시도
- 풀해상도 컬러 RGB8은 1GigE 대역폭을 초과할 수 있습니다. fps가 안 나오면
  프레임레이트를 낮추거나 픽셀 포맷/해상도를 조정하세요.
