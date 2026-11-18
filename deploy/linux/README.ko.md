# Linux 호스트에 플랫폼 설치하기

새 **Ubuntu LTS** 호스트를 위한 설치 런북입니다. 플랫폼은 호스트 프로세스 두 개(API와 워커, systemd 유닛)이며, Traefik 컨테이너가 TLS를 종단하는 하나의 TLS 오리진 뒤에서 동작합니다. 컨테이너화되어 있지 않습니다. 필요한 파일은 모두 이 디렉터리에 있습니다.

| 파일 | 설명 |
|---|---|
| `forge-api.service`, `forge-worker.service` | 두 개의 유닛 |
| `build.sh` | `constraints.txt` 기반 venv, 프런트엔드 빌드, 이미지 빌드, `alembic upgrade head` |
| `traefik/run-traefik.sh`, `traefik/forge-platform.yml.template` | 기준 Traefik: 정적 플래그와 플랫폼 오리진용 file-provider 라우트 |
| `traefik/make-selfsigned-cert.sh` | 테스트 설치용 자체 서명 인증서 |

문서 전체에서 바꿔야 하는 placeholder: `forge.example.com`(플랫폼 호스트), `apps.forge.example.com`(앱 기본 도메인), `/opt/forge`(체크아웃 경로), 사용자 `forge`. 체크아웃 경로나 사용자를 바꾸면 두 유닛 파일에서도 함께 바꿔야 합니다.

1단계는 `sudo`가 필요합니다. 2~7단계는 `forge` 사용자로 실행하고, 8단계와 10단계의 `systemctl` 줄은 관리자 계정으로 실행합니다(`forge` 사용자에게는 `sudo`가 없습니다).

## 운영 전 필수 설정과 권장 설정

세 그룹으로 나뉩니다. 첫째는 코드가 강제하고, 둘째는 해당 기능을 쓰는 순간 실패하며, 셋째는 무언가 고장 나기 전까지 아무 신호도 없습니다.

**없으면 시작이 거부됨**(`FORGE_ENV=prod`; API, 워커, 모든 명령이 똑같이 거부하며 우회 플래그는 없습니다). 각 행은 `backend/app/config.py`의 가드에 해당합니다.

| 설정 | 없을 때 | 설명 위치 |
|---|---|---|
| `FORGE_ENV` | 설정되지 않으면 다른 값이 모두 있어도 거부됩니다. 기본값이 없습니다(`Settings.model_post_init`의 첫 검사) | 4단계 |
| `DATABASE_URL` | 미설정, 빈 값, SQLite URL이면 거부됩니다. prod에는 Postgres가 필요합니다(`model_post_init`, `ProdDatabaseUrlMissingError`) | 1.5단계, 4단계 |
| `SESSION_SIGNING_SECRET`, `PREVIEW_SIGNING_SECRET` | 미설정, 빈 값, 32바이트 미만이면 거부됩니다(`_require_prod_signing_secret`) | 4단계 |
| `SERVICE_ENV_ENCRYPTION_KEY` | 미설정 또는 빈 값이면 거부됩니다(`model_post_init`, `ProdServiceEnvEncryptionKeyError`) | 4단계 |
| `PLATFORM_PUBLIC_URL` | 미설정 또는 빈 값이거나 `https://` 오리진이 아니면 거부됩니다(`model_post_init`; `_check_platform_public_url`) | 0단계, 4단계 |
| `AUTH_MODE` | prod에서 `dev`는 거부됩니다(`ProdDevAuthModeError`). `oidc`는 `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`이 필요합니다(`OidcConfigMissingError`) | 0단계, 4단계 |
| `BOOTSTRAP_ADMIN_EMAILS`, `BOOTSTRAP_ADMIN_PASSWORD` | `AUTH_MODE=local`일 때: 이메일은 정확히 하나, 비밀번호는 설정되어 있고 dev 기본값이 아니며 12자 이상이어야 합니다(`LocalBootstrapAdminEmailCountError`, `ProdWeakBootstrapPasswordError`) | 0단계, 4단계 |

직접 설정한 경우에만 해당하지만 시작 시 거부되는 항목도 있습니다. `*`, `null`, 순수 오리진이 아닌 값, 또는 `APPS_BASE_DOMAIN` 하위인 `CORS_ORIGINS` 항목(`_check_cors_origin`), 그리고 prod에서 `https://`가 아닌 `NOTIFY_WEBHOOK_URL`이나 `NOTIFY_SMTP_TLS=none`(`_check_notify_settings`)입니다.

**플랫폼은 시작되지만 핵심 기능이 실패함:**

| 설정 | 없을 때 | 설명 위치 |
|---|---|---|
| `SERVICE_POSTGRES_ADMIN_DSN`과 서비스 Postgres | 어떤 서비스도 배포할 수 없습니다. 확정 단계가 거부합니다(`PROVISION_UNCONFIGURED`) | 3단계 |
| 첫 번째 팀, `python -m app.teams create` | 프로젝트를 만들 수 없습니다. prod 설치는 팀을 시드하지 않습니다 | 9단계 |
| `FRONTEND_DIST_DIR`과 프런트엔드 빌드 | API는 응답하지만 웹 UI가 없습니다 | 2단계, 4단계 |
| 엔진 API 키(`ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY`) | 빌드가 에이전트를 실행하지 못합니다 | 0단계, 4단계 |
| `APPS_BASE_DOMAIN`, `TRAEFIK_ENABLED`, Traefik 컨테이너 | 배포된 서비스와 프리뷰에 라우트가 생기지 않습니다 | 4단계, 5단계 |
| 호스트의 서비스 Redis 이미지(`SERVICE_REDIS_IMAGE`) | 첫 배포가 실패하고 프리뷰가 표시되지 않습니다 | 2단계, 7단계 |

**권장:**

| 설정 | 없을 때 | 설명 위치 |
|---|---|---|
| `NOTIFY_WEBHOOK_URL`, 또는 `NOTIFY_SMTP_*`와 `NOTIFY_EMAIL_TO` | 플랫폼은 동작하지만, 관리자 콘솔을 지켜보거나 `/api/ready`를 직접 조회하지 않는 한 서비스나 워커가 멈추거나 디스크가 부족해도 아무도 알림을 받지 못합니다 | `.env.example` |
| `/api/ready`에 대한 readiness 모니터 | 사용자가 신고해야만 장애를 알게 됩니다. 이 엔드포인트는 200 또는 503으로 답하고 `database`와 `worker` 중 해당하는 것을 알려 줍니다 | `.env.example`(`READINESS_TIMEOUT_SECONDS`) |
| 백업: `SERVICE_DATA_BACKUP_DIR`, 플랫폼 데이터베이스, `backend/local-git`, `SERVICE_ENV_ENCRYPTION_KEY` | 호스트를 잃기 전까지는 문제가 없지만, 잃는 순간 플랫폼이 백업하지 않는 모든 것을 잃습니다 | 11단계 |

## 0. 먼저 결정할 것

- **DNS와 TLS.** `forge.example.com`과 `*.apps.forge.example.com`이 이 호스트로 해석되어야 하고, 인증서는 둘 다 포함해야 합니다. 기준 구성은 두 이름에 인증서 하나를 사용합니다. 조직의 CA, ACME, 또는 테스트용 `make-selfsigned-cert.sh`를 쓰십시오. TLS는 필수입니다. prod에서 세션 쿠키는 `Secure`이고 `__Host-` 접두사가 붙으므로 평문 HTTP로는 로그인이 되지 않으며, 시작 시 `https`가 아닌 `PLATFORM_PUBLIC_URL`은 거부됩니다.
- **로그인.** `AUTH_MODE=oidc`(IdP의 `OIDC_*` 값이 필요) 또는 `AUTH_MODE=local`(12자 이상 비밀번호를 가진 부트스트랩 관리자).
- 빌드에서 에이전트를 실행하려면 **엔진 API 키**(`ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY`).

## 1. 사전 준비 (sudo)

블록을 순서대로 붙여 넣으십시오.

### 1.1 패키지

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git openssl python3 python3-venv nodejs npm postgresql
node --version      # needs 22 or newer (CI builds with 22); if older, install Node 22 from your organisation's mirror or NodeSource
python3 --version   # should match the minor in the header of backend/constraints.txt (3.14 at the time of writing)
```

### 1.2 Docker Engine

Docker의 apt 저장소를 사용합니다(최신 방법은 docs.docker.com/engine/install/ubuntu 참조). Docker가 아직 해당 릴리스를 게시하지 않았다면 `sudo apt-get install -y docker.io`가 대안입니다.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<DOCKER_REPO
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
DOCKER_REPO
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
```

### 1.3 Docker 데몬 설정

배포되는 모든 서비스는 전용 사설 네트워크를 받는데, Docker 기본 풀은 약 30개만 담습니다. 아래 `default-address-pools`는 4096개(`10.64.0.0/12`를 /24 단위로)를 담습니다. 사내 네트워크와 겹치지 않는 대역을 고르십시오. 로그 상한은 수다스러운 컨테이너가 디스크를 채우는 것을 막습니다. preflight는 이 풀을 `SERVICE_DATA_EXPECTED_SERVICES`와 대조해 검사합니다.

`"bip"`은 기본 브리지 `docker0`을 `172.17.0.1/24`로 고정합니다. 반드시 설정하십시오. `bip`이 없으면 `default-address-pools`가 있을 때 `docker0`이 풀의 첫 서브넷(`10.64.0.1/24`)을 차지하고, `172.17.0.1`에 바인드하는 API 유닛이 `Errno 99 cannot assign requested address`로 실패합니다. `bip`은 풀 바깥이면서 사용 중인 모든 네트워크 바깥이어야 합니다.

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "bip": "172.17.0.1/24",
  "default-address-pools": [{ "base": "10.64.0.0/12", "size": 24 }],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl enable docker
sudo systemctl restart docker
sudo docker info --format '{{json .DefaultAddressPools}}'
ip -4 addr show docker0        # must show 172.17.0.1/24
```

API는 이 주소에 바인드합니다(8단계). 다른 `bip`을 골랐다면 `sudo systemctl edit forge-api`로 유닛을 오버라이드하고 `[Service]` / `Environment=FORGE_API_BIND=<docker0 address>`를 지정하며, Traefik에는 `FORGE_API_ADDR=<address>:8000`을 지정하십시오. `FORGE_API_BIND`를 `.env`에 넣지 마십시오. `Settings`가 알 수 없는 키를 거부합니다.

### 1.4 사용자와 권한

```bash
sudo useradd --system --create-home --home-dir /home/forge --shell /bin/bash forge
sudo usermod -aG docker forge
sudo install -d -o forge -g forge /opt/forge /etc/forge /etc/forge/certs
```

**`docker` 그룹은 root와 동등합니다.** Docker 소켓과 통신할 수 있는 사람은 호스트의 `/`를 마운트하는 privileged 컨테이너를 띄울 수 있습니다. 따라서 `forge` 사용자, API, 워커는 설계상 이 호스트의 root 권한을 가집니다(ADR 0002: 호스트가 신뢰 경계). 다른 사람을 이 그룹에 추가하지 말고, root에게 줄 수 없는 로그인을 `forge` 계정에 주지 말며, `/opt/forge/backend/.env`는 `0600` 모드로 유지하십시오.

### 1.5 플랫폼 데이터베이스 (호스트의 Postgres)

플랫폼 자체 저장소이며, 3단계의 서비스 Postgres와는 별개입니다(ADR 0023 §Amendment 7). URL 특수문자가 없는 비밀번호를 쓰십시오. hex가 가장 간단합니다. 4단계에서 쓰도록 보관하십시오.

```bash
PLATFORM_DB_PASSWORD="$(openssl rand -hex 24)"
sudo -u postgres psql -v ON_ERROR_STOP=1 \
  -c "CREATE ROLE forge LOGIN PASSWORD '$PLATFORM_DB_PASSWORD'" \
  -c "CREATE DATABASE forge OWNER forge"
echo "$PLATFORM_DB_PASSWORD"    # copy it; it goes into DATABASE_URL
```

호스트 Postgres는 기본적으로 `127.0.0.1:5432`에서 대기하며, 플랫폼에는 그것으로 충분합니다.

## 2. 코드 받기와 빌드

`forge` 사용자로(`sudo -iu forge`):

```bash
git clone <your-repo-url> /opt/forge     # or copy a release checkout into it
cd /opt/forge
deploy/linux/build.sh --skip-migrate
```

이 명령은 `backend/constraints.txt`로 `backend/.venv`를 만들고, `frontend/dist`를 빌드하고, `forge-llm-proxy:latest`와 `forge-agent-sandbox:latest`를 빌드하며, 서비스 Redis 이미지(`SERVICE_REDIS_IMAGE`, 기본 `redis:8-alpine`)를 pull합니다(플랫폼은 프로젝트별 Redis를 pull 없이 생성하므로, 이 이미지가 없는 호스트는 첫 배포가 실패하고 프리뷰도 표시되지 않습니다). OpenAI 엔진의 샌드박스가 필요하면 `--with-openai-image`를 추가하십시오. 마이그레이션은 하지 않습니다. 데이터베이스는 `.env`가 먼저 있어야 합니다. 기본값이 아닌 이미지 이름(`AGENT_SANDBOX_IMAGE` 등)을 쓴다면 같은 Dockerfile로 해당 이미지를 직접 빌드하십시오. 기본값이 아닌 `SERVICE_REDIS_IMAGE`는 `backend/.env`에 들어간 뒤에 `build.sh`가 pull하므로, 설정 후 `build.sh --skip-migrate`를 다시 실행하십시오. 워커의 디스크 상한 점검은 범용 `SANDBOX_IMAGE`(기본 `python:3.11-slim`)로 컨테이너를 만들며 pull은 하지 않습니다. `build.sh`가 에이전트 이미지를 이 이미지 `FROM`으로 빌드하므로 호스트에 이미 있고, `python -m app.preflight`(7단계)가 있는지 확인합니다. 다른 `SANDBOX_IMAGE`를 쓴다면 직접 `docker pull`하십시오.

## 3. 서비스 Postgres

생성된 모든 서비스의 데이터베이스가 올라가는 공유 인스턴스입니다. 반드시 **이 Docker 데몬 위의** 컨테이너여야 하고(플랫폼이 서비스별 네트워크에 연결합니다), loopback에만 publish하며, superuser가 아닌 `CREATEDB CREATEROLE` 롤을 가져야 합니다(ADR 0023 §Amendment 5, 6). `max_connections`는 `SERVICE_DATA_EXPECTED_SERVICES x SERVICE_DATA_CONNECTIONS_PER_SERVICE + 20`으로 잡으십시오(기본값으로 100 x 5 + 20 = 520).

```bash
SERVICE_PG_BOOTSTRAP_PASSWORD="$(openssl rand -hex 24)"
SERVICE_PG_ADMIN_PASSWORD="$(openssl rand -hex 24)"
docker run -d --name forge-service-postgres --restart unless-stopped \
  -e POSTGRES_PASSWORD="$SERVICE_PG_BOOTSTRAP_PASSWORD" \
  -p 127.0.0.1:55432:5432 \
  -v forge-service-postgres-data:/var/lib/postgresql \
  postgres:18-alpine postgres -c max_connections=520
until docker exec forge-service-postgres pg_isready -h 127.0.0.1 -U postgres; do sleep 1; done
docker exec forge-service-postgres psql -U postgres \
  -c "CREATE ROLE forge_admin WITH LOGIN PASSWORD '$SERVICE_PG_ADMIN_PASSWORD' CREATEDB CREATEROLE NOSUPERUSER"
echo "$SERVICE_PG_ADMIN_PASSWORD"   # copy it; it goes into SERVICE_POSTGRES_ADMIN_DSN
```

(1.4단계 이후 `forge` 사용자는 `docker` 그룹이 적용되도록 새로 로그인, 즉 `sudo -iu forge`를 해야 합니다.) 데이터는 이름 있는 볼륨 `forge-service-postgres-data`에 있으며, 이 볼륨을 `docker volume rm`하면 모든 서비스 데이터베이스가 사라집니다. 부트스트랩 superuser 비밀번호는 다시 필요하지 않으며 `.env`에 넣지 않습니다.

## 4. `.env`

```bash
cd /opt/forge/backend
cp .env.example .env
chmod 600 .env
```

`.env`를 편집하십시오. `.env.example`에는 `FORGE_ENV=dev`와 `CORS_ORIGINS=http://localhost:5173` 두 개의 활성 줄이 있습니다. **`FORGE_ENV`는 그 자리에서 바꾸고**(`FORGE_ENV` 줄을 하나 더 추가하는 것은 해결책이 아닙니다) `CORS_ORIGINS`는 그대로 두십시오. systemd와 앱의 dotenv 파서가 이 파일을 둘 다 읽고 서로 다르게 해석하므로 규칙이 있습니다. 한 줄에 `KEY=value` 하나, 값 뒤에 주석(`# comment`)을 붙이지 않음, 그리고 **어떤 값에도 `$`, 따옴표(`'` 또는 `"`), 백슬래시, 앞뒤 공백을 넣지 않음**. 생성한 시크릿은 안전합니다. 운영자가 직접 정한 값(`BOOTSTRAP_ADMIN_PASSWORD`, `OIDC_CLIENT_SECRET`, API 키)은 확인하십시오.

시크릿 세 개를 생성합니다(각각 서로 달라야 하며 재사용 금지).

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"                                 # SESSION_SIGNING_SECRET
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"                                 # PREVIEW_SIGNING_SECRET
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"    # SERVICE_ENV_ENCRYPTION_KEY
```

**`SERVICE_ENV_ENCRYPTION_KEY`는 지금 데이터베이스와 따로 떨어진 곳에 백업하십시오.** 잃어버리면 저장된 모든 서비스 환경변수를 복구할 수 없습니다.

이 구성에 필요한 값입니다. `FORGE_ENV=prod`에서는 다음이 없으면 시작이 거부됩니다. `DATABASE_URL`, `SESSION_SIGNING_SECRET`, `PREVIEW_SIGNING_SECRET`, `SERVICE_ENV_ENCRYPTION_KEY`, `https://`로 시작하는 `PLATFORM_PUBLIC_URL`, `dev`가 아닌 `AUTH_MODE`와 그 로그인 값(아래), 그리고 `local`이라면 12자 이상의 부트스트랩 비밀번호. 나머지는 이 런북의 구성에 필요한 값입니다.

```
FORGE_ENV=prod
DATABASE_URL=postgresql://forge:<PLATFORM_DB_PASSWORD>@127.0.0.1:5432/forge
PLATFORM_PUBLIC_URL=https://forge.example.com
SESSION_SIGNING_SECRET=<generated>
PREVIEW_SIGNING_SECRET=<generated>
SERVICE_ENV_ENCRYPTION_KEY=<generated>
FRONTEND_DIST_DIR=../frontend/dist
SERVICE_POSTGRES_ADMIN_DSN=postgresql://forge_admin:<SERVICE_PG_ADMIN_PASSWORD>@127.0.0.1:55432/postgres
APPS_BASE_DOMAIN=apps.forge.example.com
TRAEFIK_ENABLED=true
TRAEFIK_ENTRYPOINT=websecure
SERVICE_URL_SCHEME=https
TRAEFIK_METRICS_PORT=8082
GIT_PROVIDER=local
USE_SANDBOX_EXECUTOR=true
ANTHROPIC_API_KEY=<your key>
```

여기에 로그인 모드를 더합니다. `oidc`는 `AUTH_MODE=oidc`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`(리다이렉트 URL은 `OIDC_REDIRECT_URL`을 지정하지 않으면 `PLATFORM_PUBLIC_URL`에서 유도됩니다), 그리고 `BOOTSTRAP_ADMIN_EMAILS=<first admin's email>`. `local`은 `AUTH_MODE=local`, `BOOTSTRAP_ADMIN_EMAILS=<one email>`, `BOOTSTRAP_ADMIN_PASSWORD=<12+ characters>`(행이 처음 만들어질 때 한 번만 읽힙니다).

`GIT_PROVIDER=local`은 모든 서비스의 코드를 `backend/local-git`에 보관하며, 그러면 그것이 유일한 사본입니다(11단계). 인증서가 5단계의 정적 인증서라면 `TRAEFIK_CERT_RESOLVER`는 비워 두고, ACME resolver를 쓸 때만 설정하십시오. 그 밖의 모든 설정은 `.env.example`에 설명되어 있습니다.

## 5. Traefik

preflight가 Traefik 존재 여부를 점검하므로 7단계보다 먼저 합니다. 인증서는 플랫폼 호스트와 `*.<APPS_BASE_DOMAIN>`을 모두 포함해야 합니다. 테스트용 자체 서명은 다음과 같습니다.

```bash
cd /opt/forge/deploy/linux/traefik
./make-selfsigned-cert.sh forge.example.com apps.forge.example.com /etc/forge/certs
FORGE_PLATFORM_HOST=forge.example.com FORGE_CERT_DIR=/etc/forge/certs ./run-traefik.sh
```

직접 준비한 인증서를 쓴다면 `tls.crt`와 `tls.key`를 `/etc/forge/certs`에 두십시오. 스크립트는 플랫폼 라우트를 `/etc/forge/traefik/dynamic/forge-platform.yml`(Traefik이 감시)에 생성하고, host-gateway 매핑, `:443`의 TLS, 이로 리다이렉트하는 `:80`, loopback 전용 metrics 엔트리포인트를 갖춘 `forge-traefik` 컨테이너를 시작합니다. `forge` 사용자가 소유한 `/etc/forge` 아래에 쓰기 때문입니다(1.4단계). Traefik 자체 access log는 꺼 둔 채로 두십시오. 프리뷰와 콜백 라우트는 자격 증명이 URL에 실려 오므로 특히 꺼 두어야 합니다. ACME는 Traefik 정적 플래그(스크립트 편집)에 resolver를 정의하고, 템플릿과 `TRAEFIK_CERT_RESOLVER`에 그 이름을 지정하십시오.

## 6. 마이그레이션

업그레이드라면 먼저 플랫폼 데이터베이스를 백업하십시오. 첫 설치에서는 비어 있습니다.

```bash
cd /opt/forge && deploy/linux/build.sh --migrate-only
```

## 7. Preflight

```bash
cd /opt/forge/backend && .venv/bin/python -m app.preflight
```

읽기 전용이며, 점검마다 PASS, WARN, FAIL을 출력하고 PASS가 아닌 줄 아래에 해결 방법을 보여 줍니다(Docker 접근 가능 여부, host-gateway 매핑을 갖춘 Traefik, 서비스 Postgres와 그 롤, 서비스 Redis 이미지를 포함한 이미지, 스키마가 head인지, 공개 오리진, 주소 풀). 계속하기 전에 모든 FAIL을 해결하십시오. 유닛과 같은 사용자로, `backend/`에서 실행하십시오.

## 8. 유닛

```bash
sudo cp /opt/forge/deploy/linux/forge-api.service /opt/forge/deploy/linux/forge-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forge-api forge-worker
systemctl status forge-api forge-worker --no-pager
```

워커는 단 한 번만 활성화하십시오(ADR 0038). API는 Docker 브리지 주소 `172.17.0.1:8000`에 바인드하며, 이는 Traefik에서 `host.docker.internal`이 가리키는 주소입니다. 호스트 밖에서는 접근할 수 없지만 **이 데몬의 브리지에 있는 모든 컨테이너가 접근할 수 있습니다**(Traefik만이 아닙니다). 그래서 API는 모든 라우트에서 자체 인증을 유지합니다. 유닛의 기본값은 1.3단계의 `bip`과 일치하며, 다른 값을 쓴다면 거기의 오버라이드를 보십시오. 로그: `journalctl -u forge-api -u forge-worker -f`.

## 9. 첫 로그인 확인

```bash
curl -sk --resolve forge.example.com:443:127.0.0.1 https://forge.example.com/api/health
```

`{"status":"ok"}`가 나와야 합니다(DNS가 이미 호스트를 가리킨다면 `--resolve`는 빼십시오). 브라우저에서 `https://forge.example.com/`을 열고 부트스트랩 관리자로 로그인하십시오. 새 prod 설치에는 팀이 없고 프로젝트를 만들려면 팀이 필요하므로 먼저 만드십시오. `forge` 사용자로 `backend/`에서 실행합니다.

```bash
cd /opt/forge/backend && .venv/bin/python -m app.teams create "<team name>"
```

그다음 프로젝트를 만들고, 빌드를 승인을 거쳐 배포까지 진행해 보십시오. 배포된 서비스는 `https://<project-id>.apps.forge.example.com/`에서 응답합니다. 자체 서명 인증서는 브라우저에서 한 번 예외 처리가 필요합니다(또는 `curl -k`).

## 10. 업그레이드

```bash
sudo systemctl stop forge-worker forge-api
# back up the platform database first (pg_dump forge), then:
cd /opt/forge && git pull            # or deploy the new checkout
deploy/linux/build.sh                # venv, frontend, images, migrate
cd backend && .venv/bin/python -m app.preflight
```

**빌드 이미지 정리(sweep) 이전 버전에서 업그레이드할 때 한 번만:** 이전에 만들어진 빌드 이미지에는 라벨이 없어서 sweep이 절대 지우지 않습니다. 워커를 계속 정지한 상태에서(위의 preflight와 아래의 시작 사이에 해당합니다) 더 이상 어떤 빌드도 필요로 하지 않는 이 설치의 이미지를 나열하고(보존 규칙은 sweep과 같은 `keep_build_ids`), 목록을 읽어 본 뒤, 정확히 그 태그만 강제 없이 삭제하십시오.

```bash
cd /opt/forge/backend
.venv/bin/python -c "
from app.db import SessionLocal
from app.models.build import Build
from app.services.image_retention import keep_build_ids
from sqlalchemy import select
with SessionLocal() as db:
    keep = keep_build_ids(db)
    for build_id, ref in db.execute(select(Build.id, Build.image_ref).where(Build.image_ref.is_not(None))):
        if build_id not in keep:
            print(ref)
" > /tmp/forge-old-images.txt
cat /tmp/forge-old-images.txt        # read it: these are this installation's tags only
xargs -r -n1 docker image rm < /tmp/forge-old-images.txt
```

이 스크립트는 `.env`가 가리키는 데이터베이스를 대상으로 하므로 이 설치의 태그만 나열합니다. `-f` 없는 `docker image rm`은 컨테이너가 아직 쓰는 이미지를 거부하며, 그것이 올바른 동작입니다. 이름 접두사(`forge-build-*`)로 삭제하거나 `docker image prune`을 쓰지 마십시오. 공유 엔진에서는 다른 설치의 이미지까지 지워집니다.

그런 다음 새 코드로 서비스를 시작합니다.

```bash
sudo systemctl start forge-api forge-worker
```

마이그레이션 전에 워커를 멈추고 새 코드로 다시 시작하십시오. 이전 워커가 새 워커와 나란히 실행되어서는 안 됩니다(ADR 0038). 릴리스가 Traefik 플래그를 바꾼다면 이후에 `run-traefik.sh --recreate`를 다시 실행하십시오.

## 11. 백업

무엇을 누가 백업하는지, 호스트를 잃어도 무엇이 남는지는 다음과 같습니다. 플랫폼은 서비스 데이터베이스를 일정에 따라 `SERVICE_DATA_BACKUP_DIR`에 덤프합니다(백업이 커버하는 마운트를 가리키게 하십시오). **운영자가** 플랫폼 데이터베이스, `backend/local-git`(`GIT_PROVIDER=local`일 때), `SERVICE_ENV_ENCRYPTION_KEY`를 백업해야 하며, 마지막 것은 데이터베이스 옆에 두지 마십시오.

## WSL2 대용 환경 (테스트 전용)

`/etc/wsl.conf`에 `systemd=true`가 설정된 WSL2 위의 Ubuntu 배포판은 이 런북을 그대로 실행할 수 있으며, 다음 차이가 있습니다.

- Docker Engine은 **배포판 안에** 설치하십시오(1.2). 그 배포판에 대해 Docker Desktop의 WSL integration은 **꺼 두십시오**(Docker Desktop, Settings, Resources, WSL integration). 켜면 Docker Desktop의 `docker` shim과 Engine의 소켓이 섞입니다. Docker Desktop의 `docker` shim이 `PATH`에 남아 있어도 `/usr/bin/docker`가 먼저 오므로 그쪽이 우선합니다.
- WSL2 네트워킹은 기본이 NAT입니다. 다른 머신에서는 이 배포판에 접근할 수 없지만 Windows의 `localhost`는 배포판으로 포워딩됩니다. `*.localtest.me` 이름(예: `PLATFORM_PUBLIC_URL=https://forge.localtest.me`, `APPS_BASE_DOMAIN=apps.localtest.me`)은 `hosts` 편집 없이 loopback으로 해석됩니다. WSL 안에서는 `::1`로 해석되는데, Traefik이 두 스택 모두에 publish하므로 동작합니다.
- `/mnt/c`에서 `forge`로 clone하려면 `git config --global --add safe.directory <path>/.git`이 필요합니다.
- 배포판은 유휴 상태가 되면 멈추며, 이때 두 유닛도 함께 내려갑니다. 예를 들어 Windows에서 `wsl -d <distro> -- sleep infinity`를 계속 실행해 두는 식으로 유지하십시오.
- `systemd-binfmt`는 시작 시 실패하지만 무해합니다.
- 커널은 Microsoft 것입니다. `bip`을 설정하면(1.3) `docker0`은 실제 호스트처럼 `172.17.0.1`이 되지만, 그래도 `ip -4 addr show docker0`으로 확인하십시오.
- 프로덕션 호스트가 아니라 대용 환경입니다. 내구성 있는 디스크도 백업 체계도 없고, Windows 쪽에서 그 안의 모든 것에 접근할 수 있습니다.
