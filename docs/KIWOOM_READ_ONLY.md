# 키움 조회 전용·Execution Guardian 사용 안내

최종 갱신: 2026-08-31

## 현재 가능한 것

- 키움 REST API OAuth 접근 토큰을 메모리에만 발급
- 국내주식 계좌평가잔고 `kt00018`, 종목정보 `ka10001`, 미체결 `ka10075`, 체결 `ka10076` 조회
- 계좌번호를 마스킹한 불변 로컬 스냅샷과 감사 이벤트 저장
- 공식 시세 그림자 체결과 미션 관리 포지션·실계좌 대사
- 격리된 주문 자격증명 변수와 세션을 사용하는 지정가 매수·매도 `kt10000/kt10001`, 취소 `kt10003` Execution Guardian
- R0→R1→L1→L2 게이트, L1 5분 승인, L2 제한 자동화, 킬 스위치와 자동 강등

조회 클라이언트에는 주문 메서드와 범용 요청 탈출구가 없다. 주문 어댑터는 별도 클래스·별도 환경변수만 사용하며 시장가·신용·미수·공매도·파생상품을 지원하지 않는다. 현재 저장소 기본값은 R0, 키 없음, `KIWOOM_TRADING_ENABLED=false`, `MONEYGUN_RELEASE_APPROVED=false`이므로 실주문 호출은 불가능하다.

## 로컬 설정

1. 키움 REST API 사용 등록과 실전 App Key를 발급한다. 이 프로젝트는 사용자 결정에 따라 모의 App Key를 사용하지 않는다.
2. `.env.example`을 `.env`로 복사하고 아래 값만 로컬에서 입력한다. 키를 채팅·문서·Git에 넣지 않는다.

```dotenv
KIWOOM_ENVIRONMENT=production
KIWOOM_APP_KEY=로컬에서만_입력
KIWOOM_SECRET_KEY=로컬에서만_입력
KIWOOM_ACCOUNT_ALIAS=키움 주계좌
KIWOOM_ORDER_ENVIRONMENT=production
KIWOOM_ORDER_APP_KEY=
KIWOOM_ORDER_SECRET_KEY=
KIWOOM_TRADING_ENABLED=false
MONEYGUN_RELEASE_APPROVED=false
MONEYGUN_OWNER_API_TOKEN=
MONEYGUN_LIVE_CAPITAL_KRW=100000
```

3. 환경 파일을 읽어 API를 시작한다.

```powershell
.venv\Scripts\python -m uvicorn moneygun_api.main:app --app-dir apps/api/src --env-file .env --port 8000
```

4. 웹의 `그림자 주문`에서 `상태 새로고침`, `계좌 동기화`, 공식 시세 저장과 계좌 대사를 순서대로 확인한다.

현재 로컬 `.env`는 `KIWOOM_ENVIRONMENT=production`이며 실전 운영계좌를 조회만 한다. 이 설정은 주문 권한을 열지 않는다.

키움은 등록 계좌에 발급한 실전 App Key/Secret으로 OAuth 토큰을 만들고 조회·주문 TR을 호출한다. Signal Guild는 같은 키를 사용할 수 있더라도 읽기 프로세스가 주문 권한을 얻지 않도록 `KIWOOM_ORDER_*`에 별도 바인딩하고 자동 fallback을 금지한다. 주문 바인딩과 소유자 토큰은 장애 훈련·`릴리스 점검`·사용자 활성화 지시 뒤에만 설정한다. 토큰은 24자 이상이어야 하며 화면 입력값은 새로고침 시 사라진다.

## API 경계

- `GET /v1/brokers/kiwoom/status`: 키 존재 여부와 허용·차단 기능만 반환
- `POST /v1/brokers/kiwoom/account-snapshots/sync`: 계좌평가잔고 조회 후 마스킹 저장
- `POST /v1/brokers/kiwoom/quotes/{symbol}/sync`: 공식 종목 시세 저장
- `POST /v1/reconciliations/run`: 미션 관리 포지션과 실계좌 대사
- `GET /v1/shadow/orders`: 그림자 주문과 이벤트 조회
- `POST /v1/shadow/orders`: 멱등 키로 그림자 주문안 생성
- `POST /v1/shadow/orders/{id}/simulate`: 다음 장 가상 체결 또는 취소
- `GET /v1/execution/status`: 다중 잠금, 서버 제어, 승격 게이트와 사고 표시
- `POST /v1/execution/intents`: 공식 READY 주문안의 실주문 사전점검
- `POST /v1/execution/intents/{id}/approval`: L1 범위 고정 5분 승인
- `POST /v1/execution/intents/{id}/submit`: 모든 잠금과 신선도 재검사 후 1회 제출
- `POST /v1/execution/intents/{id}/cancel`: 접수·부분체결 주문의 별도 취소 상태 기계

## 다음 안전 게이트

소프트웨어 경로 구현은 완료했지만 운용 증거는 완료되지 않았다. 승인된 실데이터 OOS, R1 60거래일·100결정, 대사 100%, 네트워크 단절·부분체결·토큰 만료·거래정지 훈련, 비밀 회전, 운영 PostgreSQL, 관측·알림·백업과 별도 `릴리스 점검`이 남았다. 조건을 테스트 데이터로 채우거나 DB를 직접 수정해 승격해서는 안 된다.
