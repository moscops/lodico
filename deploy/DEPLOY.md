# EC2 배포 가이드 (Ubuntu 24.04 / 26.04 LTS 기준)

봇은 디스코드로 **나가는** 연결만 쓰므로 인바운드 포트는 SSH(22)만 있으면 됩니다.

## 1. 인스턴스 만들기

- AMI: Ubuntu Server 24.04 LTS 또는 26.04 LTS (AMI 목록에 있는 최신 LTS), 타입: `t3.micro` (또는 `t4g.nano/micro`, ARM도 무방)
- 스토리지: 기본 8~10GB
- 보안 그룹 인바운드: **SSH(22) — 내 IP만**. 다른 포트는 열지 않습니다.
- 키 페어를 만들어 `.pem` 파일을 안전한 곳에 보관합니다.
- AWS Budgets에서 월 예산 알림을 걸어 두세요.

## 2. 서버 준비 (SSH 접속 후)

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv python3-pip sqlite3
sudo timedatectl set-timezone Asia/Seoul   # 선택: 로그 시각을 KST 로 (봇 동작에는 영향 없음)
```

## 3. 코드 올리기 (내 PC에서)

`.venv`, `data`, `__pycache__` 는 올리지 않습니다. PowerShell 예시:

```powershell
cd C:\Users\User\Desktop\로디코
# 필요한 것만 묶기
tar --exclude=.venv --exclude=data --exclude=__pycache__ --exclude=.env -czf raidbot.tgz bot tests config.toml requirements.txt README.md deploy .env.example
scp -i 키.pem raidbot.tgz ubuntu@<EC2 공인IP>:~
scp -i 키.pem .env ubuntu@<EC2 공인IP>:~/raidbot.env
```

기존 일정 데이터를 그대로 옮기려면 봇을 끈 상태에서 `data/bot.db` 도 올립니다.

```powershell
scp -i 키.pem data\bot.db ubuntu@<EC2 공인IP>:~/bot.db
```

## 4. 설치 (서버에서)

```bash
mkdir -p ~/raidbot && tar -xzf ~/raidbot.tgz -C ~/raidbot
cd ~/raidbot
mkdir -p data
mv ~/raidbot.env .env && chmod 600 .env
[ -f ~/bot.db ] && mv ~/bot.db data/bot.db      # 기존 데이터를 옮길 때만

python3 --version                       # 3.11 이상이어야 함
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -t .   # 선택: 설치 확인 (전부 OK 면 정상)
```

`.env` 의 `DB_PATH` 는 비워 두면 `data/bot.db` 를 씁니다. 구글 시트 내보내기를 쓰면 키 파일을 `data/` 안에 올리고
`GOOGLE_CREDENTIALS_FILE=/home/ubuntu/raidbot/data/키파일.json` 으로 적습니다 (`chmod 600`).

**중요:** 같은 봇 토큰으로 내 PC와 EC2를 동시에 켜지 마세요. 명령어에 두 번 응답합니다. 옮긴 뒤 PC 쪽 봇은 끄세요.

## 5. systemd 로 상시 실행

```bash
sudo cp ~/raidbot/deploy/raidbot.service /etc/systemd/system/raidbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now raidbot     # 부팅 시 자동 시작 + 지금 시작
```

`User=ubuntu`, 경로 `/home/ubuntu/raidbot` 가 다르면 서비스 파일을 고치세요 (Amazon Linux는 `ec2-user`).

자주 쓰는 명령:

```bash
sudo systemctl status raidbot          # 상태
journalctl -u raidbot -f               # 실시간 로그 (Ctrl+C 로 종료)
journalctl -u raidbot --since today    # 오늘 로그
sudo systemctl restart raidbot         # 재시작 (config.toml/.env 수정 후)
```

로그에 `로그인: <봇이름>` 이 나오면 성공입니다. 로스트아크 점검 중이면 "자동 레벨 갱신 실패" 경고가 나오지만 봇은 정상입니다.

## 6. 백업 (하루 한 번)

```bash
chmod +x ~/raidbot/deploy/backup.sh
~/raidbot/deploy/backup.sh && ls ~/raidbot/data/backups     # 한 번 실행해서 확인
( crontab -l 2>/dev/null; echo '30 4 * * * /home/ubuntu/raidbot/deploy/backup.sh' ) | crontab -
```

매일 04:30에 `data/backups/` 로 14일치를 보관합니다. 디스크가 사라지는 경우에 대비해 S3 백업도 권장합니다:
S3 버킷을 만들고, EC2에 S3 쓰기 권한이 있는 **IAM 역할**을 붙인 뒤 (액세스 키를 서버에 저장하지 마세요),
AWS CLI 설치(최근 우분투는 `apt install awscli` 가 없을 수 있으니 AWS 공식 문서의 awscli v2 설치 방법 사용) 후 crontab 의 명령 앞에 `S3_BUCKET=s3://버킷이름` 을 붙입니다.

복구: 봇을 멈추고(`sudo systemctl stop raidbot`) `gunzip -c 백업파일.db.gz > ~/raidbot/data/bot.db` 후 시작합니다.

## 7. 업데이트 배포

```bash
# PC에서 새 tgz 를 만들어 scp 한 뒤, 서버에서:
sudo systemctl stop raidbot
tar -xzf ~/raidbot.tgz -C ~/raidbot          # data/ 와 .env 는 tgz 에 없으므로 그대로 유지됨
chmod +x ~/raidbot/deploy/backup.sh          # 압축을 풀면 실행 권한이 사라져 cron 백업이 실패한다
~/raidbot/.venv/bin/pip install -r ~/raidbot/requirements.txt
sudo systemctl start raidbot
journalctl -u raidbot -n 30
```

새 슬래시 명령어가 추가된 배포는 시작할 때 자동으로 디스코드에 등록됩니다 (`DEV_GUILD_ID` 가 있으면 그 서버에 즉시).

## 8. 점검 목록

- [ ] 보안 그룹에 22번 외 인바운드가 없다 (SSH는 내 IP만)
- [ ] `.env`, 구글 키 파일 권한이 600이다
- [ ] PC의 봇을 껐다 (토큰 중복 사용 방지)
- [ ] `systemctl enable` 로 부팅 자동 시작이 걸려 있다
- [ ] 백업이 한 번 이상 성공했다
- [ ] AWS 예산 알림이 설정돼 있다
