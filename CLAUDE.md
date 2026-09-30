# 로디코 — 로스트아크 공대 일정 조율 디스코드 봇

공대원의 캐릭터·가능 시간을 모아 레이드 일정을 자동 제안·관리하고, 알림·완료 표시·시트 연동까지 처리하는 discord.py 봇.
기능 명세와 명령어 목록은 `README.md`, 배포 절차는 `deploy/DEPLOY.md`. 이 파일은 **이어서 작업할 때 필요한 맥락**만 적는다.

## 빠른 시작

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # DISCORD_TOKEN 등은 직접 채운다 (아래 '비밀' 참고)
.venv\Scripts\python -m unittest discover -s tests -t .    # 전부 OK 여야 한다 (현재 105개)
.venv\Scripts\python -m bot                                  # 실행
```

- 파이썬 3.11+ (운영 서버 3.14). 의존성: discord.py, python-dotenv, openpyxl, gspread.
- 테스트는 디스코드에 접속하지 않는다. cog 는 `tests/test_bot_load.py` 가 로드·직렬화·도움말 길이까지 검사한다.

## 구조

- `bot/db.py` SQLite 스키마·쿼리 (마이그레이션은 `MIGRATIONS` 딕셔너리에 컬럼 추가). 서버(guild)별 데이터는 `guild_id`/`members.guild_id` 로 분리.
- `bot/services.py` 규칙·집계 (참가 검사 `check_join`, 골드 계획, `build_overview`). **디스코드와 무관**해서 테스트 대상.
- `bot/planner.py` 자동 일정 제안. `bot/parties.py` 고정 파티(색·주간 자동 생성). `bot/sheet.py` 엑셀 양식 생성·파싱.
- `bot/session_edit.py` `/공대일정수정`·`/일정완료` 의 규칙(권한·시각 계산·완료 표시). `bot/gsheet.py` 구글 시트 내보내기(표 만들기 / gspread 쓰기 분리).
- `bot/render.py` 메시지 문자열 생성. `bot/devseed.py` 테스트용 더미 공대원 도구.
- `bot/cogs/*.py` 슬래시 명령어·버튼 UI. 명령어는 `meta(...)`(extras)로 `/도움` 이 자동 생성된다 — **새 명령어에는 `meta` 필수**(테스트가 검사).
- `config.toml` 레이드·숙제 구간·기본값. 패치 때 `/레이드 수정` 또는 파일 수정 후 `/레이드 새로고침`.

## 코드 규칙 / 결정 사항

- 사용자에게 보이는 문구는 한국어, 시간은 KST 고정 오프셋(`timeutil.KST`). 규칙 위반은 `RuleError`(메시지를 그대로 보여 줌).
- 완료 상태: `sessions.cleared` — 1 완료 / 0 미완료(못 깸 → 그 레이드는 다시 "안 잡힌 숙제") / NULL 자동(일정 끝 시각이 지나면 완료). 판정은 `render.session_done/session_failed`.
- 같은 시간대 다른 일정 참가는 **막지 않고 경고만**(공대장 요청). 같은 캐릭터의 같은 레이드 주 1회는 그대로 막는다(단, 미완료 표시한 일정은 제외).
- `/일정등록` 시각을 비우면 `config.toml` 의 `default_start`(21:00)로 만들고 메모에 "⏱️ 시간 미정" 표시. 엑셀 고정 파티는 요일만 적어도 그 기본 시각으로 생성.
- `/공대일정수정`: 공대장·서버 관리 권한자는 모든 일정, 그 외는 **내 캐릭터가 참가한 일정만**(시각·메모도 참가자면 수정 가능 — 참가자 전원 멘션 공지). 일정 25개 초과는 페이지 넘김.
- 파티 색: `party_raid_id` 연결, 없으면 메모 "파티 N" 으로 폴백(`db.party_numbers_by_session`).
- 버튼 UI 는 `discord.ui.DynamicItem`(custom_id 에 일정 번호·동작 이름)이라 재시작해도 옛 메시지 버튼이 동작한다. `/메뉴` 는 그 버튼 5개를 채널에 고정.
- 로스트아크 API 점검(503)이어도 봇은 정상 기동한다. 자동 레벨 갱신만 건너뛰고 다음에 재시도. 키가 없어도 나머지는 동작.
- 구글 시트 내보내기는 시트 1개에 쓰므로 **서버 1곳 전제**(여러 서버면 서로 덮어씀). 아직 키 미설정 상태.
- `DEV_GUILD_ID` 는 **비워 둠**(여러 서버에서 쓰도록 명령어 전역 등록 → 반영에 최대 1시간).

## 운영 환경 (AWS EC2)

- 인스턴스 `lodico`(서울 리전, Ubuntu 26.04, 파이썬 3.14). 접속 키는 로컬 `Desktop\keys\lodico.pem`(저장소에 없음). **공인 IP 는 인스턴스를 중지/시작하면 바뀌므로** 콘솔에서 확인. 보안 그룹은 SSH(22) 내 IP 만.
- 서버 경로 `/home/ubuntu/raidbot` (`.env`, `data/bot.db`, `.venv`), systemd 서비스 `raidbot`. 로그: `journalctl -u raidbot -f`. 매일 04:30 `deploy/backup.sh`(14일 보관, cron).
- 운영 DB 는 **서버에 있다**(로컬 `data/` 는 개발용 사본). 서버 봇과 로컬 봇을 **같은 토큰으로 동시에 켜지 말 것**(이중 응답). 개발용은 별도 디스코드 앱(토큰)+`DB_PATH` 를 권장.
- 서버 디스코드 서버 ID(운영): `1554459685648859196`.

### 배포 요령 (Windows PowerShell 5.1 에서 검증된 방식)

1. `tar --exclude=.venv --exclude=data --exclude=__pycache__ --exclude=.env -czf raidbot.tgz bot tests assets config.toml requirements.txt README.md deploy .env.example`
2. `scp`/`ssh` 는 PATH 에 없을 수 있어 `C:\Windows\System32\OpenSSH\ssh.exe` 전체 경로로 부른다. 키 권한은 `icacls`로 본인만 읽기.
3. 서버에서 여러 줄 명령은 **스크립트 파일을 scp 로 올려 `bash 파일`** 로 실행한다(따옴표 문제, PowerShell 5.1 이 stdin 에 BOM 을 붙이는 문제 회피).
4. 순서: DB 백업 → 압축 해제(`.env`·`data` 는 tgz 에 없어 유지) → `chmod +x deploy/backup.sh`(압축을 풀면 실행 권한이 사라져 cron 백업이 깨진다) → `pip install -r` → 서버에서 테스트 → `sudo systemctl restart raidbot` → 로그에 `로그인:` 확인.

## 현재 상태 (마지막 세션 기준)

- README 로드맵 1~4단계 + 1.5단계 구현 완료. 이후 추가: `/불가`(주간 예외), 리셋 전날 미완료 알림, 구글 시트 내보내기, 참가/빠지기 버튼, `/공대일정수정`, `/일정완료`, `/메뉴`, 일정별 기본 보기.
- **로컬에는 있으나 서버에 미배포**: `/메뉴` 명령어와 `/도움` 상단 "자주 쓰는 기능". (그 이전 변경은 모두 배포됨.)
- **서버 DB 에 테스트용 더미 공대원 8명(더미1~8)이 들어 있다.** 지우기: 서버에서 `cd ~/raidbot && .venv/bin/python -m bot.devseed remove --guild 1554459685648859196`. 더미는 discord_id 가 `9e18` 이상이라 실제 유저와 섞이지 않는다.
- 중복이던 고정 파티 일정 #26~36 은 취소함(기록은 남김). 그 일정의 참가자는 재배정이 필요할 수 있다.
- 미해결/아이디어: 리셋 전날 알림 끄기 설정, 참석률 기록(불참·지각), 알림 채널 여러 개, 구글 시트 실제 연결 검증, S3 백업.

## 비밀 — 저장소에 없는 것

`.env`(DISCORD_TOKEN, LOSTARK_API_KEY, 구글 키 경로), `data/`(DB·백업), `*.pem`, 구글 서비스 계정 JSON 은 `.gitignore` 로 제외된다. 새 컴퓨터에서는 이 파일들을 안전한 경로로 직접 옮기거나 다시 발급한다.

## 작업 시 주의 (겪은 함정)

- 셸 heredoc 으로 파이썬 코드를 생성하면 문자열 안의 `\n` 이스케이프가 실제 줄바꿈으로 바뀌어 `unterminated string literal` 이 난 적이 있다. 코드 수정은 편집 도구로 하고, 수정 후 항상 전체 테스트를 돌린다.
- 슬래시 명령어 인자 이름은 한글이다(`일정`, `결과` 등). 버튼 메뉴(`cogs/menu.py`)는 명령어의 `callback` 을 한글 키워드 인자로 직접 부른다.
- 새 슬래시 명령어는 디스코드에 반영되기까지 몇 분~1시간이 걸릴 수 있다(전역 등록).
