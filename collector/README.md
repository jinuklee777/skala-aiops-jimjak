# 짐작 수집기

인천국제공항공사 「항공기 수하물 정보(조업사용)」 API([data.go.kr 15144042](https://www.data.go.kr/data/15144042/openapi.do))의 도착편 응답을 5분마다 받아 원본 그대로 쌓는다.
이 API는 오늘·내일 것만 준다. 수집이 멈춘 시간의 데이터는 다시 구할 수 없다.

- 표준 라이브러리만 쓴다(Python 3.9 이상). 팀 저장소의 다른 파일은 건드리지 않는다.
- 명령은 저장소 루트에서 `python -m collector <명령>`으로 부른다.

## 처음 한 번

1. data.go.kr에서 활용신청을 하고, 마이페이지의 일반 인증키를 `collector/.env`에 넣는다(`collector/.env.example` 참고). `.env`는 커밋되지 않는다.
2. 한 번 받아 본다: `python -m collector run`
3. 5분마다 돌게 등록한다 → [자동 실행](#자동-실행)
4. 알림을 설정하고 `python -m collector notify-test`로 시험한다 → [멈춤 알림](#멈춤-알림)

## 명령

| 명령 | 하는 일 |
|---|---|
| `tick` | 할 때가 된 수집을 하고 멈춤·일일 보고를 확인한다. launchd·cron이 5분마다 부른다 |
| `run [arrivals\|arrivals_slave]` | 지금 한 번 받는다 |
| `status [--date 2026-10-12]` | 최근 실행, 오늘 호출 수, 하루 요약 |
| `asof "2026-10-12 14:00" [--date 2026-10-13] [--json]` | 그 시각에 알고 있던 편별 상태(CSV) |
| `history KE902 [--date 2026-10-12]` | 한 편의 값이 언제 어떻게 바뀌었나 |
| `rebuild` | 원본 파일로 편별 상태 표를 다시 만든다 |
| `notify-test` | 웹훅·하트비트를 시험한다 |

## 무엇을 언제 받나

| 작업 | 요청 | 주기 | 하루 호출 |
|---|---|---|---|
| `arrivals` | 도착편, 오늘·내일(`searchDay=0`), Master 행만(`codeshare=MA`), 한 번에 2,000행 | 5분마다 | 288 |
| `arrivals_slave` | 같은 요청의 코드쉐어 Slave 행 | 06:00, 18:00 | 2 |

### 호출 간격 근거

- 개발계정 한도는 하루 500회다.
- 2026-10-12 21시 실측: 오늘·내일 Master 행은 1,105행이고, `numOfRows=2000`이면 한 번에 다 온다(0.9초, 압축 44KB). 수집 한 번이 호출 한 번이다.
- 5분 간격이면 하루 288회 + Slave 2회 = 290회다. 남는 210회는 재시도 몫이다.
- 간격이 촘촘할수록 "그때 무엇을 알고 있었나"를 정확하게 복원할 수 있다. 한도 안에서 가장 촘촘한 5분으로 했다.
- 재시도는 한 쪽에 최대 3번(5초, 20초 뒤)이다. 일일 한도 초과(22)나 키 오류(30 등)처럼 다시 불러도 같은 오류는 재시도하지 않는다. 직전 실행도 실패했으면 장애가 이어지는 중으로 보고 한 번씩만 부른다. 재시도로 한도를 다 써서 장애가 끝난 뒤에 못 받는 일을 막는다.
- Master만 받는 이유: Slave 행은 Master와 수하물 값이 같다(사전 조사: 다른 사람이 공개한 실제 응답 5,486건 분석). 편마다 공동운항 편 수만 필요해서 Slave는 하루 두 번 받는다.
- `COLLECTOR_INTERVAL_MINUTES`를 10으로 늘리면 23:30~00:30만 5분 간격으로 받는다. 자정에 전날 편이 응답에서 빠지면서 23시대 도착편의 마지막 짐 시각을 놓치기 쉬워서다.

### 같은 것을 두 번 받거나 쓰지 않게

- **잠금:** 한 번에 하나만 수집한다. 앞 수집이 안 끝났는데 다시 불리면 건너뛴다.
- **슬롯:** 수집마다 맡는 구간(5분)이 있고, 이미 받은 구간은 다시 받지 않는다. 실패한 구간은 그 안에서 한 번만 더 시도한다. 컴퓨터가 잠들어 놓친 구간은 건너뛰고 지금 구간만 받는다. 지나간 시각의 응답은 다시 받을 수 없어서다.
- **편 상태 표:** 값이 바뀐 편만 새 행을 쓴다.

## 저장 구조

```
collector/data/                       COLLECTOR_DATA_DIR로 바꿀 수 있다
├── raw/<작업>/<날짜>/<실행>-p<쪽>-a<시도>.json.gz     받은 응답 그대로
└── collector.db                      SQLite
```

원본 파일 하나가 호출 하나다. 실패한 응답(HTTP 500, 게이트웨이 오류 XML)도 남긴다. 파일 안은 이렇다.

```json
{"format": 1, "collector": "0.1.0", "job": "arrivals", "run": "arrivals-20261012T211256",
 "slot": "2026-10-12T21:10:00+09:00", "operation": "getFltArrivalsBaggageClaimDesk",
 "params": {"type": "json", "searchDay": "0", "codeshare": "MA", "numOfRows": "2000", "pageNo": "1"},
 "page": 1, "attempt": 1, "requested_at": "2026-10-12T21:12:56+09:00", "elapsed_ms": 873,
 "http_status": 200, "headers": {"Content-Type": "application/json;charset=UTF-8", "...": "..."},
 "body": "{\"response\":{\"header\":{\"resultCode\":\"00\", ... (응답 본문 그대로)"}
```

- 서비스키는 원본·DB·로그 어디에도 남기지 않는다.
- 크기는 5분 간격에서 하루 약 13MB, 1년 약 4.6GB다(압축 기준).
- 시각은 모두 한국 시각(+09:00)이다.

SQLite 표:

| 표 | 내용 |
|---|---|
| `runs` | 수집 1회: 작업, 구간, 시작·끝, 결과(`ok` 빠짐없이 / `partial` 일부만 / `failed` 못 받음), 행 수, 새 편·바뀐 편·사라진 편 |
| `calls` | 호출 1회: 쪽, 시도, HTTP 상태, 결과 코드, 행 수, 원본 파일 위치, 오류 |
| `snapshots` | 편 × 값이 바뀐 시각. 응답 항목 JSON 그대로. `present = 0`은 그때 응답에서 사라졌다는 표시 |
| `latest` | 편별 최신 상태. 처음 본 때, 마지막으로 본 때, 마지막으로 바뀐 때. 응답에서 사라진 편도 마지막 값을 남긴다 |
| `notices` | 알림 상태(같은 알림을 되풀이하지 않게) |

편 키는 `편명|도착 예정일시`다(예: `KE902|202610122340`).

## 시점 복원

시각 T의 상태는 T 이전 마지막 수집에서 본 값이다. 그 수집에서 응답에 없던 편은 뺀다.

```bash
python -m collector asof "2026-10-12 14:00" --date 2026-10-12 > state.csv
```

Python에서:

```python
from collector import db, flights
from collector.config import load_config

conn = db.connect(load_config().db_path)
states = flights.state_as_of(conn, "arrivals", "2026-10-12T14:00:00+09:00")
# states[i].flight_key, .observed_at(그 값을 처음 본 수집 시각), .item(응답 항목 그대로)
```

- T 직전에 수집이 끊겼으면 값이 오래됐을 수 있다. `asof`가 함께 알려 주는 "그 전 마지막 수집" 시각을 본다.
- "응답에서 사라짐"은 빠짐없이 받은 수집에서만 판정한다. 편 수가 직전의 절반 아래로 급감하면 응답 이상으로 보고 판정하지 않는다.
- 편 상태 표는 원본에서 만든 것이다. 규칙을 바꾸거나 DB가 망가지면 `rebuild`로 원본에서 다시 만든다.
  서버에는 원본이 최근 30일 치만 있으니, 서버에서 `rebuild`하기 전에는 Drive에서 원본 전체를 받는다([복구](#복구)). 안 받고 돌리면 30일보다 오래된 편 이력이 표에서 사라진다.

## 멈춤 알림

두 가지를 같이 쓴다.

| 방식 | 언제 알리나 | 설정 |
|---|---|---|
| 하트비트 | 마지막 성공 뒤 신호가 끊기면. **컴퓨터가 꺼지거나 잠들어도 알린다** | [healthchecks.io](https://healthchecks.io)(무료)에서 체크를 만들고 Period 5분, Grace 55분으로 둔다. 핑 주소를 `COLLECTOR_HEARTBEAT_URL`에 넣는다. 알림 받을 곳(이메일·텔레그램·슬랙 등)은 그쪽에서 고른다 |
| 웹훅 | 수집 멈춤(마지막 성공 뒤 60분, 이어지면 6시간마다), 재개, 매일 09시 전날 보고 | 슬랙이나 디스코드 웹훅 주소를 `COLLECTOR_WEBHOOK_URL`에 넣는다 |
| 알림 명령 | 웹훅과 같다 | `COLLECTOR_NOTIFY_COMMAND`에 명령을 넣으면 메시지를 마지막 인자로 붙여 실행한다. 종료 코드 0이면 보낸 것으로 본다. 메신저 봇 스크립트로 개인 메시지를 보낼 때 쓴다 |

웹훅과 알림 명령은 수집기가 돌아야 보낼 수 있다. 컴퓨터가 꺼진 경우는 하트비트만 잡는다. 보낸 알림의 원문은 수집기 로그에도 남는다.

일일 보고는 이렇게 생겼다(`status`에도 나온다).

```
[짐작 수집기] 10/12(월) 수집 보고
- 회차: 성공 1 · 일부 0 · 실패 0 · 빠짐 33 / 예정 34
- 호출: 4회 (하루 한도 500)
- 도착 예정 568편: 결항 0, 수취대 미배정 7, 도착 519편 중 마지막 짐 시각 확보 497편 (96%)
- 23시대 도착 0편 중 마지막 짐 시각 확보 0편
- 원본: 0.1MB · 디스크 여유 3.2GB
⚠ 받은 회차(1)가 예정(34)의 90%에 못 미침
```

경고는 받은 회차가 예정의 90% 미만, 일부만 받은 회차가 10% 초과, 편 수가 지난 7일 중앙값의 70% 미만, 호출이 한도의 90% 초과, 디스크 여유가 2GB 미만일 때 붙는다.

## 자동 실행

### 맥

launchd가 5분마다 `tick`을 부른다. 노트북을 덮으면 멈추니 상시 서버로 옮기기 전까지 쓴다.

```bash
sed -e "s|__REPO__|$PWD|g" -e "s|__PYTHON__|$(which python3)|g" -e "s|__HOME__|$HOME|g" \
    collector/deploy/jimjak.collector.plist > ~/Library/LaunchAgents/jimjak.collector.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/jimjak.collector.plist
```

- 로그: `~/Library/Logs/jimjak-collector.log`
- 멈추기: `launchctl bootout gui/$(id -u)/jimjak.collector`

### 리눅스 서버

2026-10-12부터 GCP VM(us-central1, Debian 12)에서 cron으로 돈다. 미국 리전에서도 data.go.kr 호출이 된다.

1. 코드만 받는다.
   ```bash
   git clone --depth 1 --filter=blob:none --sparse -b collector https://github.com/jinuklee777/skala-aiops-jimjak.git
   cd skala-aiops-jimjak && git sparse-checkout set collector
   ```
2. `collector/.env`를 옮기고 `chmod 600 collector/.env`.
3. 이미 받은 데이터가 있으면 `collector/data/`를 통째로 옮긴다. DB는 다른 곳에서 쓰는 중이 아닐 때 `PRAGMA wal_checkpoint(TRUNCATE)`를 한 뒤 옮긴다. 원본만 있어도 `rebuild`로 표를 다시 만들 수 있다.
4. `collector/deploy/crontab.example`의 경로를 고쳐 `crontab -e`에 붙인다.
5. 같은 키로 두 곳에서 동시에 돌리지 않는다. 하루 호출 한도를 넘는다.

코드를 고치면 서버에서 `git pull`로 반영한다.

## 백업과 복구

서버의 원본과 DB를 하루 한 번 Google Drive로 백업한다(`collector/deploy/backup.sh`, [rclone](https://rclone.org/drive/) 사용). 서버 디스크가 망가지거나 VM을 지워도 수집한 이력은 남는다.

| 무엇 | Drive 위치 | 보관 |
|---|---|---|
| 원본 | `jimjak-collector/raw/<작업>/<날짜>/` | 전부. 서버에서 지운 파일도 Drive에는 남는다 |
| DB 스냅샷 | `jimjak-collector/db/collector-<날짜>.db.gz` | 최근 14일(`BACKUP_DB_KEEP_DAYS`). 지난 것은 Drive 휴지통으로 간다 |

- 서버에는 원본을 최근 30일(`BACKUP_KEEP_DAYS`) 치만 둔다. 그보다 오래된 날짜 폴더는 Drive에 있는 것과 MD5까지 같은지 `rclone check`로 확인한 뒤에만 지운다. 다르면 지우지 않고 알린다.
- DB 스냅샷은 SQLite 백업 API로 뜬다. 수집이 돌고 있어도 한 시점의 일관된 사본이 된다. 올리기 전에 `PRAGMA quick_check`로 검사한다.
- 실패하면 수집기 알림(웹훅·알림 명령)으로 "백업 실패"를 보낸다. 로그는 `~/jimjak-backup.log`.
- 서비스키(`.env`)와 rclone 토큰은 백업하지 않는다. 서비스키는 data.go.kr 마이페이지에서 다시 볼 수 있다.

### 처음 한 번

1. Google Cloud 콘솔에서 Drive API를 켜고 OAuth 클라이언트(데스크톱 앱)를 만든다. 범위는 `drive.file`이다. rclone이 만든 파일만 보여서 Drive의 다른 파일은 건드리지 못한다.
   동의 화면은 「프로덕션」으로 게시한다. 테스트 상태로 두면 토큰이 7일 뒤 만료되어 백업이 멈춘다.
2. 서버에서 `rclone config`로 `gdrive` 리모트를 만든다. 서버에는 브라우저가 없으니 자동 인증 물음에 `n`을 답한다.
   그러면 `rclone authorize ...` 줄이 나온다. 이 줄을 브라우저가 있는 컴퓨터에서 실행하고, 받은 토큰을 서버에 붙인다([rclone 원격 설정](https://rclone.org/remote_setup/)).
3. `rclone mkdir gdrive:jimjak-collector`. `drive.file` 범위라 Drive 웹에서 직접 만든 폴더는 rclone이 못 본다.
4. `collector/deploy/backup.sh`를 한 번 손으로 돌려 보고, `crontab.example`의 백업 줄을 `crontab -e`에 붙인다.

### 맞는지 확인

```bash
rclone check collector/data/raw gdrive:jimjak-collector/raw --one-way --exclude "*.tmp"
rclone lsl gdrive:jimjak-collector/db
```

`--one-way`는 서버에 있는 파일이 Drive에 모두 같은 내용으로 있는지만 본다. Drive에만 있는 오래된 원본은 정상이다. 마지막 백업 뒤에 받은 원본은 다음 백업 전까지 차이로 나온다.

### 복구

DB가 망가졌을 때(저장소 루트에서):

```bash
crontab -e    # 수집 줄을 주석 처리해 잠시 멈춘다
rclone copy gdrive:jimjak-collector/raw collector/data/raw    # 서버에서 지운 30일 지난 원본까지 받는다
rclone lsf gdrive:jimjak-collector/db                         # 가장 최근 스냅샷 이름을 본다
rclone copyto gdrive:jimjak-collector/db/collector-2026-10-13.db.gz /tmp/collector.db.gz
mv collector/data/collector.db collector/data/collector.db.broken
rm -f collector/data/collector.db-wal collector/data/collector.db-shm
gunzip -c /tmp/collector.db.gz > collector/data/collector.db
python3 -m collector rebuild  # 스냅샷 뒤에 받은 원본까지 편 상태 표에 넣는다
crontab -e    # 주석을 푼다
```

- `rebuild`는 편 상태 표(`snapshots`·`latest`)만 다시 만든다. 실행 기록(`runs`·`calls`)은 스냅샷을 뜬 시점까지만 남는다.
- 서버를 통째로 잃었으면 [리눅스 서버](#리눅스-서버) 1~2단계로 코드와 `.env`를 놓는다. 그다음 위 「처음 한 번」 2단계로 rclone을 다시 연결하고, 위 명령으로 원본과 DB를 받는다. 받은 뒤 cron을 등록한다.

## 필드 확인 결과

| 질문 | 답 | 근거 |
|---|---|---|
| 응답에 출발편도 섞이나 | 아니다. 도착(`getFltArrivalsBaggageClaimDesk`)과 출발(`getFltDeparturesBaggageClaimDesk`)이 다른 오퍼레이션이다. 수집기는 도착만 받는다 | Swagger 명세 |
| 적재대는 무엇인가 | 출발편 응답에만 있는 `lateral1·2`와 그 상태·시작·종료 시각이다. 출발 수하물을 싣는 곳이라 도착편 처리 단계와 관계없다. 포털의 응답 항목 목록에는 두 오퍼레이션 것이 합쳐져 있다 | Swagger 명세 |
| 착륙 시각이 있나 | 있다. `landingDatetime`(활주로 착륙 일시)이고 착륙 뒤에 채워진다 | Swagger 명세, 실측 |
| 기준 시각 후보 | ① `landingDatetime` 착륙 ② `estimatedDatetime` 탑승교 접현 예정. 도착 뒤에는 실제 접현 시각이 되는 것으로 보여 블록인에 가장 가깝다 ③ `scheduleDatetime` 예정 | Swagger 명세, 사전 조사(②의 도착 뒤 의미는 추정) |
| 예측 입력에 쓰면 안 되는 값 | `landingDatetime`, `bagFirstTime`, `bagLastTime`, `bagRemark`, `runway`, 대부분의 `gateNo`. 도착 전에는 비어 있다 | 사전 조사(공개 응답 5,486건 분석) |

2026-10-12 21시 첫 실측:

- 응답 모양은 `{"response": {"header": {...}, "body": {"numOfRows", "pageNo", "totalCount", "items": [...]}}}`다. `items`는 목록, 쪽 정보는 정수, 항목 필드는 27개다.
- 값은 모두 문자열이고 없으면 빈 문자열이다. `remark`, `gateNo`, `tmp1`, `tmp2`만 null이 온다.
- `codeshare=MA` 필터가 듣는다(1,105행 모두 Master). `numOfRows=2000`도 된다.
- `searchDay=0` 응답에 10/11 23:55 예정편이 하나 있었다(예상 도착 10/12 00:02). 오늘·내일을 가르는 기준이 예정 시각이 아니라 예상 시각일 수 있다(1건이라 추정).
- 내일 도착편 536편 중 530편은 전날 21시에 이미 수취대가 배정돼 있었다.
- Slave 행은 1,243행이었다.

아직 확인할 것:

- 자정에 전날 편이 언제 빠지는지(23:30~00:30 원본으로 확인)
- `bagLastTime`이 언제 채워지고 나중에 바뀌는지
- 날마다 마지막 짐 시각을 얼마나 확보하는지(일일 보고)

쌓은 데이터를 공개할 때는 출처(인천국제공항공사, 공공데이터포털)를 적는다.

## 테스트

```bash
python -m unittest discover -s collector/tests -t .
```

네트워크 없이 로컬 가짜 서버로 돈다. `tests/fixtures/recorded_arrivals_*.json.gz`는 2026-10-12에 실제로 받은 응답이고, 나머지 표본은 실제 응답과 필드·형식을 맞춰 만든 것이다.
