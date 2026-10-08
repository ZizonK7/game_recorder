# LoL Recorder

리그 오브 레전드 게임을 자동으로 녹화하고, 이벤트(킬/데스/오브젝트)를 재생바에 표시하고,
경기가 끝나면 Riot API로 데이터를 받아 대시보드와 raw 데이터 내보내기를 제공하는 Windows 프로그램.

## 주요 기능

| 기능 | 설명 |
|---|---|
| 자동 녹화 | 롤 게임이 시작되면 자동으로 녹화, 끝나면 자동 저장 (리플레이/관전은 녹화 안 함) |
| 랭크만 녹화 | 설정에서 켜면 솔로랭크/자유랭크만 녹화 |
| 이벤트 재생바 | 내 킬·데스·어시스트, 멀티킬, 드래곤/바론/전령/유충, 포탑/억제기 등을 마커로 표시. 클릭하면 5초 전부터 재생 |
| 데스 리플레이 | 죽으면 죽기 8초 전 ~ 2초 후 장면을 게임 위 작은 창으로 즉시 재생 (길이·위치·크기 설정 가능) |
| 게임 소리만 녹음 | 롤 프로세스의 소리만 캡처. 디스코드·음악·마이크는 녹음되지 않음 |
| 경기 데이터 | 게임 종료 후 Match-v5 경기 상세 + 타임라인을 자동 저장. 와드/아이템/레벨업 이벤트도 재생바에 추가 |
| 대시보드 | 승률, KDA, CS/분, 분당 딜량·골드, 시야, 킬 관여율, 경기별 추세, 챔피언별 기록, 라인전 골드 차이 |
| Raw 내보내기 | 원본 JSON + 분석용 CSV (participants / teams / frames / events / recording_events) |
| 영상 자동 정리 | 최근 N경기 영상만 보관 / 최대 저장 용량. 오래된 영상부터 지우고 경기 기록·통계는 유지 |

## 게임 성능

녹화가 게임 성능을 잡아먹지 않도록:

- **화면 캡처**: Windows Desktop Duplication(ddagrab)으로 GPU 메모리에서 바로 캡처
- **인코딩**: 그래픽카드 하드웨어 인코더(NVIDIA NVENC / AMD AMF / Intel QuickSync) 자동 선택. CPU 인코딩은 최후의 수단
- **축소(720p)**: 가능하면 GPU에서 처리
- **우선순위**: 녹화 프로세스와 앱 모두 '낮음' 우선순위로 실행
- **기본값**: 720p · 30fps · 5Mbps (30분 게임 약 1GB)

### 인코더 관련 참고

- **NVIDIA 그래픽카드는 최신 드라이버가 필요합니다.** 번들된 FFmpeg 이 요구하는 NVENC 버전보다 드라이버가
  오래되면 NVENC 를 쓰지 못하고 (`%APPDATA%\LoLRecorder\lolrec.log` 에 `Driver does not support the required
  nvenc API version` 기록), CPU 내장그래픽(AMD/Intel)이나 CPU 인코딩으로 넘어갑니다. 녹화는 되지만 더 무거우므로
  NVIDIA App 에서 드라이버를 업데이트하세요. 업데이트 후 다음 실행 때 자동으로 NVENC 를 다시 탐지합니다.
- 어떤 인코더를 쓰는지는 녹화 중 상태 표시줄의 `[nvenc-gpu]`, `[amf-cpuscale]` 같은 표시로 확인할 수 있습니다.
- 데스 리플레이는 녹화를 2초 조각으로 나눠 저장하는 것에 의존합니다. 녹화 폴더의 `segments` 에 `seg_00000.ts`
  하나만 계속 커지고 `segments.csv` 가 비어 있다면 인코더가 2초마다 키프레임을 만들지 않는 것이니 이슈로 알려 주세요.
  (0.1.0 의 AMD AMF 인코더에서 이 문제가 있었고 수정되었습니다.)

## 설치

GitHub Actions에서 빌드된 파일을 받습니다 (Actions → 최신 실행 → Artifacts).

- `LoLRecorder-Setup-x.y.z.exe` : 설치 파일 (관리자 권한 불필요)
- `LoLRecorder-portable-x.y.z.zip` : 압축 풀고 `LoLRecorder.exe` 실행

요구 사항: Windows 10 2004(빌드 19041) 이상, 64비트.

- 게임 소리만 녹음하는 기능은 Windows 의 Process Loopback API 를 사용합니다. Microsoft 문서상 최소 버전은
  빌드 20348(Windows 11 / Server 2022 계열)이지만, OBS 의 "응용 프로그램 오디오 캡처"와 마찬가지로
  Windows 10 2004 이상에서도 동작하는 것으로 알려져 있습니다.
- 이 기능을 쓸 수 없는 환경에서는 자동으로 **소리 없이 영상만** 녹화하고 알림을 띄웁니다.

## 처음 설정

1. 프로그램 실행 → **설정 → Riot API** 에 personal API 키 입력 → "키 확인"
2. (선택) Riot ID(이름#태그) 입력. 비워두면 롤 클라이언트에서 자동으로 감지
3. 롤에서 **테두리 없는 창 모드**를 권장 (전체 화면에서는 데스 리플레이 창이 보이지 않음. 녹화는 정상)
4. 게임을 하면 자동으로 녹화됩니다. 창을 닫아도 트레이에서 계속 실행됩니다.

과거 경기 데이터가 필요하면 **과거 경기 가져오기** (영상 없이 데이터만).

- API 키가 없어도 **영상 녹화와 이벤트 재생바(킬/데스/오브젝트)는 동작**합니다. 키는 경기 상세 데이터,
  대시보드, 와드/아이템/레벨업 이벤트, 과거 경기 가져오기에 필요합니다. personal 키는 만료되면
  설정에서 새 키로 바꿔 주세요.
- **녹화 범위**: 게임 창이 아니라 **설정한 모니터 전체**를 녹화합니다. 게임 중 Alt+Tab 으로 띄운
  채팅·브라우저·알림도 영상에 담길 수 있습니다. (소리는 게임 소리만 녹음됩니다.)

## 저장 위치

- 녹화: `내 동영상\LoLRecorder\<날짜>_<챔피언>\video.mp4` + `events.json`
- 원본 API 데이터: `내 동영상\LoLRecorder\data\matches\`, `data\timelines\`
- 설정/DB/로그: `%APPDATA%\LoLRecorder\`
- API 키: Windows 자격 증명 관리자 (파일에 저장되지 않음)

설정에서 저장 위치를 바꾸면 원본 API 데이터는 새 위치로 옮겨지고, 기존 녹화 영상은 원래 폴더에 남습니다.
다른 프로그램이 파일을 사용 중이라 일부를 옮기지 못하면 기존 위치에서 계속 읽고, 다음 실행 때 다시 옮깁니다.

## 녹화 보관과 자동 삭제

설정 → 녹화 에서 두 가지 자동 삭제를 함께 쓸 수 있습니다. 둘 다 **영상 파일만** 지우고, 경기 기록·승패·
대시보드 통계·원본 API 데이터는 남습니다 (목록의 영상 칸이 `-` 로 바뀌고, 마우스를 올리면 이유가 보입니다).

| 설정 | 동작 |
|---|---|
| 최근 경기 영상만 보관 | 최근 N경기의 영상만 남기고, 새 녹화 정리가 끝날 때마다 이전 경기 영상을 삭제. 0 = 끄기 |
| 최대 저장 용량 | 녹화 폴더 전체(정리 전 조각 포함)가 용량을 넘으면 오래된 영상부터 삭제. 0 = 무제한 |

- 설정을 바꿔서 지금 지워질 영상이 생기면, 개수를 보여주고 확인을 받은 뒤에만 적용합니다.
  '아니요'를 누르면 자동 삭제 설정은 그대로 두고 나머지 설정만 저장합니다.
- 녹화 중·정리 중인 경기와, 실패해서 조각만 남은 녹화는 자동으로 지우지 않습니다.
- 재생 중인 영상은 지울 수 없어서 다음 정리 때 다시 시도합니다.
- 오래된 경기를 목록에서도 없애려면 목록에서 오른쪽 클릭 → **완전히 삭제** (영상, 녹화 폴더, 경기 기록,
  원본 API 데이터까지 삭제).

### 경기 도중 프로그램을 다시 켠 경우

같은 경기를 이어서 녹화하고, 목록에 `챔피언 (이어서 2)` 처럼 따로 표시됩니다. "최근 N경기" 와 대시보드에서는
같은 경기로 한 번만 셉니다.

### 디스크 공간이 부족할 때

- 녹화를 시작하기 전에 남은 공간이 부족하면 알림을 띄웁니다.
- 녹화 중 남은 공간이 1GB 아래로 떨어지면 녹화를 멈추고, 그때까지 녹화한 부분을 저장합니다.
- 영상을 합칠 공간이 없으면 녹화 조각(`segments` 폴더)을 그대로 두고 실패로 표시합니다. 공간을 확보한 뒤
  프로그램을 다시 켜면 자동으로 다시 합칩니다.
- 프로그램이 녹화/정리 도중 꺼졌어도 다음 실행 때 남은 조각으로 복구를 시도합니다.

## 내보낸 데이터 분석 예시

```python
import pandas as pd

p = pd.read_csv("participants.csv")
me = p[p.isMe]
me["csPerMin"] = (me.totalMinionsKilled + me.neutralMinionsKilled) / (me.gameDuration / 60)
print(me.groupby("championName")[["kills", "deaths", "assists", "csPerMin"]].mean())

frames = pd.read_csv("frames.csv")      # 분 단위 골드/CS/레벨/위치
events = pd.read_csv("events.csv")      # 타임라인 이벤트 전체
```

## 구조

```
lolrec/
  main.py            앱 시작
  watcher.py         게임 감지 → 녹화 → 이벤트 수집 → 정리
  recorder/ffmpeg.py FFmpeg 녹화 (2초 조각 저장, 데스 리플레이 클립, 합치기)
  recorder/audio.py  게임 소리 캡처 헬퍼 실행
  riot/local.py      롤 클라이언트 로컬 API (LCU, Live Client Data)
  riot/api.py        Riot Web API (요청 제한 준수)
  fetcher.py         경기 후 데이터 수집
  events.py          이벤트 모델/변환
  analysis.py        대시보드 지표
  exporter.py        raw 데이터 내보내기
  ui/                화면 (PySide6)
native/audio_capture/main.cpp   게임 소리만 캡처하는 헬퍼 (Process Loopback API)
packaging/                      PyInstaller / Inno Setup 설정
```

## 개발

```bash
pip install -r requirements-dev.txt
python -m lolrec          # 실행
pytest                    # 테스트
pyinstaller packaging/lolrec.spec   # exe 빌드 (vendor/ 에 ffmpeg.exe, ffprobe.exe, lol_audio_capture.exe 필요)
```

## 참고

- 롤 클라이언트의 로컬 API(LCU, Live Client Data API)와 공식 Riot API만 사용하며 게임 파일이나 메모리를 건드리지 않습니다.
- 번들된 FFmpeg은 GPL 빌드입니다 (`FFMPEG_LICENSE.txt`).
